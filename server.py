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
# See ~/borges-desk/projects/engram-logging/DESIGN.md §4.2 for the spec.
from engram_log_emitter import emit_if_initialized  # noqa: E402

# sqlite-vec (optional): SIMD-accelerated KNN over embeddings, replaces the
# O(N) pure-Python cosine loop in _semantic_search. If the extension fails to
# load (older Python build without enable_load_extension, or module absent),
# we fall back to the legacy Python cosine path — search still works, just
# slowly. Availability is checked once at module import and again per-connection.
try:
    import sqlite_vec as _sqlite_vec
    _SQLITE_VEC_IMPORT_OK = True
except Exception:  # pragma: no cover
    _sqlite_vec = None
    _SQLITE_VEC_IMPORT_OK = False

# Flipped to False at runtime if extension loading fails on the actual
# connection (some Python sqlite3 builds compile without load_extension).
_VEC_BACKEND_AVAILABLE = _SQLITE_VEC_IMPORT_OK


# ---------------------------------------------------------------------------
# Embedding manager (optional — works without sentence-transformers installed)
# ---------------------------------------------------------------------------

class EmbeddingManager:
    """Manages embedding model loading and computation.
    
    Lazy-loads the model on first use. Stores the model name in the KG's
    config to prevent mixing embeddings from different models.
    """

    def __init__(self):
        self._model = None
        self._model_name = None
        self._failed_models = set()  # cache download failures

    def _load_model(self, model_name: str):
        """Load the sentence-transformers model. Auto-downloads from HuggingFace."""
        if self._model is not None and self._model_name == model_name:
            return
        if model_name in self._failed_models:
            return  # already tried and failed
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            self._model_name = model_name
        except ImportError:
            self._model = None
            self._model_name = None
            self._failed_models.add(model_name)
        except Exception as e:
            import sys
            print(f"[engram] Failed to load embedding model '{model_name}': {e}", file=sys.stderr)
            self._model = None
            self._model_name = None
            self._failed_models.add(model_name)

    def is_available(self) -> bool:
        """Check if sentence-transformers is installed."""
        if os.environ.get("ENGRAM_NO_EMBEDDINGS"):
            return False
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    def embed(self, text: str, model_name: str) -> Optional[list[float]]:
        """Compute embedding for a text string. Returns None if unavailable."""
        if not text or not self.is_available():
            return None
        self._load_model(model_name)
        if self._model is None:
            return None
        vector = self._model.encode(text, convert_to_numpy=True)
        return vector.tolist()

    def embed_batch(self, texts: list[str], model_name: str) -> Optional[list[list[float]]]:
        """Compute embeddings for multiple texts. Returns None if unavailable."""
        if not texts or not self.is_available():
            return None
        self._load_model(model_name)
        if self._model is None:
            return None
        vectors = self._model.encode(texts, convert_to_numpy=True)
        return vectors.tolist()

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


# Global embedding manager instance
_embedder = EmbeddingManager()

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("ENGRAM_DATA_DIR", os.path.expanduser("~/.engram")))
DB_PATH = DATA_DIR / "knowledge.db"
SNAPSHOT_PATH = DATA_DIR / "graph_snapshot.md"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "session_log.md"

# Feeling-nudge marker file: written by checkpoint(nap), reflect, and the
# postcompact hook; read-and-cleared by engram_report_feeling. See spec §6
# "Marker file protocol" — drives mechanical nudge_source tagging.
FEELING_NUDGE_MARKER = DATA_DIR / "feeling-nudge-active.json"
FEELING_NUDGE_TTL_TURNS = 5
FEELING_NUDGE_SOURCES = {"post_compact", "nap_checkpoint", "dream_review"}

# ── Utility scoring (MemRL-inspired, cj_0027) ──
# USE model: bump utility directly on each USE action, α determined by the
# action's intensity-of-impression (Lei 2026-05-19 design call).

# Sandbox guard: set to True when running inside engram_sandbox() context.
# When True, _ensure_data_dir() will refuse to create ~/.engram.
_SANDBOX_ACTIVE = False


def _configure_paths(data_dir: str | Path) -> None:
    """Reconfigure ALL module-level path globals from a single data_dir.

    This is the ONLY safe way to redirect ENGRAM to a different directory.
    Manually patching individual path variables is fragile — if a new path
    is added to server.py and not patched, writes leak to production.

    Called at module load time (with env var / default), and by EngramClient
    and engram_sandbox() for test isolation.

    Incident context (ls_0004, ob_0678): a test that manually patched
    DATA_DIR and DB_PATH contaminated the production engram with 5 test nodes.
    """
    global DATA_DIR, DB_PATH, SNAPSHOT_PATH, CONFIG_PATH, LOG_PATH
    global FEELING_NUDGE_MARKER
    DATA_DIR = Path(data_dir)
    DB_PATH = DATA_DIR / "knowledge.db"
    SNAPSHOT_PATH = DATA_DIR / "graph_snapshot.md"
    CONFIG_PATH = DATA_DIR / "config.json"
    LOG_PATH = DATA_DIR / "session_log.md"
    FEELING_NUDGE_MARKER = DATA_DIR / "feeling-nudge-active.json"

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
USE_ALPHA = {
    # Tier 1 — deep cognitive load
    "derive":          0.15,
    "supersede":       0.15,
    "contradict":      0.15,
    "resolve":         0.15,
    "lesson_incident": 0.15,
    # Tier 2 — moderate engagement
    "inspect":         0.10,
    "subgraph":        0.10,
    "mention":         0.10,
    "focus":           0.10,  # focus_swap also uses this key — both outgoing+incoming sets
    "history":         0.10,
    "citation":        0.10,
    # Tier 3 — loose touch
    "focus_load":      0.05,
    "unfocus":         0.05,
}
USE_ALPHA_DEFAULT = 0.10  # Fallback for unknown actions

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
UTIL_BETA = 0.10
IMP_BETA = 0.005

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
TIER1_MULTIPLIER = 30   # raw-retrieval pool size per source = top_k × 30
TIER2_MULTIPLIER = 4    # composite-shrink target           = top_k × 4
# Tier 3 returns the caller's requested top_k.
MMR_LAMBDA = 0.9

# ── Special-type bypass (Lei 2026-05-19 the child-case fix) ─────────────────────
# Sparse anchor-types (person, definition, goal) and status-bearing types
# (axiom, contradiction, question, conjecture, lesson) skip the composite-ranking
# + MMR gauntlet: they just need similarity >= FTS_SIM_FLOOR to surface.
#
# Motivation (the child case, 2026-05-19 PM): pn_0005 has sim=0.3786 to
# "the child: Good night Borges!!!" — above the floor — but composite
# (rel=0.38 × max-1.5x amps = max 0.57) can't compete with goodnight
# observations (rel=0.7 × ~1.3 amps = ~0.91). MMR diversity discount is
# insufficient. Bypass ensures these sparse/structural nodes always surface
# when semantically relevant.
#
# Output contract: top_k is the GENERIC-result budget; special-type results
# are always-include when similarity >= FTS_SIM_FLOOR, so len(results) may
# exceed top_k.  Callers that need strict top_k can slice further.
SPECIAL_TYPES_BYPASS = frozenset({
    # Status-bearing (already in special_nodes channel of engram_surface)
    "axiom",
    "contradiction",
    "question",
    "conjecture",
    "lesson",
    # Sparse anchor-types (NEW — Lei 2026-05-19 PM)
    "definition",
    "person",
    "goal",
})

# Cap on the special-type bypass pool. Unbounded pool returns ~40 specials per
# query above FTS_SIM_FLOOR — fine for the engram_surface browse channel but
# noisy for callers that just want the most relevant anchors. Diversity-preserving
# selection (Lei 2026-05-19 PM): reserve top-1 per type that has members, then
# fill remaining slots with the next-best across types by similarity desc.
#
# Empirical justification: pre-cap special-channel partitioned metrics on v4.2
# golden set show recall 0.539 with precision 0.121 (n_returned=40). The cap
# lets the user keep most of the recall lift while restoring reasonable
# precision — each type's strongest anchor is preserved (so e.g. the child's
# pn_0005 is never starved by 9 high-sim definitions), and the rest of the
# budget goes to whichever type's secondary candidates are most relevant.
SPECIAL_POOL_CAP = 10

# ── FTS↔semantic merge (alpha #207/#208) ─────────────────────────────────
# When a node hits FTS, the bi-gram pair that fired may be on structural
# words (adverbs, modal verbs) that co-occur in a topically-unrelated node
# (the q_0077/ob_1152 failure). Semantic similarity is the authoritative
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
FTS_SIM_FLOOR = 0.30
FTS_BUMP_NORMALIZER = 16.0   # sqrt(this) = 4.0; bump = sqrt(|bm25|) / 4.0
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


def engram_sandbox(data_dir: str | Path | None = None, keep: bool = False):
    """Context manager for isolated ENGRAM testing.

    Guarantees complete path isolation — every module-level path constant is
    redirected to a temporary directory. On exit, paths are restored and the
    temp directory is cleaned up (unless keep=True).

    Usage:
        import server
        with server.engram_sandbox() as sandbox_dir:
            # All server functions now operate on sandbox_dir
            result = server.engram_add_observation(...)
            # Production ~/.engram is completely untouched

        # Paths automatically restored to production values

    Args:
        data_dir: Explicit sandbox directory. If None, creates a temp dir.
        keep: If True, don't delete the sandbox dir on exit (for inspection).

    Yields:
        Path to the sandbox data directory.

    Incident context (ls_0004): built after a test contaminated production
    engram because path constants were manually patched and one was missed.
    """
    import contextlib
    import tempfile
    import shutil

    @contextlib.contextmanager
    def _sandbox():
        global _SANDBOX_ACTIVE
        # Save original state
        original_dir = DATA_DIR
        original_sandbox = _SANDBOX_ACTIVE

        # Create sandbox directory
        if data_dir is not None:
            sandbox_path = Path(data_dir)
            sandbox_path.mkdir(parents=True, exist_ok=True)
            created_tmpdir = False
        else:
            sandbox_path = Path(tempfile.mkdtemp(prefix="engram_sandbox_"))
            created_tmpdir = True

        try:
            # Redirect ALL paths
            _configure_paths(sandbox_path)
            _SANDBOX_ACTIVE = True

            # Write default config
            if not CONFIG_PATH.exists():
                CONFIG_PATH.write_text(json.dumps({
                    "trust_pool": [],
                    "confidence_map": {
                        "hard_data": 0.95,
                        "official_statement": 0.85,
                        "attributed_analysis": 0.70,
                        "unnamed_source": 0.50,
                        "personal_communication": 0.40,
                        "editorial": 0.35,
                    },
                    "memory": {
                        "decay_base": 1.014,
                        "current_turn": 0,
                        "tier1_max_nodes": 200,
                        "tier2_max_nodes": 2000,
                    },
                    "mode": "single",
                    "counterparts": [],
                }))

            _ensure_data_dir()
            yield sandbox_path
        finally:
            # Restore original paths
            _configure_paths(original_dir)
            _SANDBOX_ACTIVE = original_sandbox
            # Clean up
            if created_tmpdir and not keep and sandbox_path.exists():
                shutil.rmtree(sandbox_path)

    return _sandbox()


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
    validate_summary_entry,
    RECALL_SUMMARY_HARD_CAP,
    RECALL_KEYWORDS_MIN,
    RECALL_KEYWORDS_MAX,
    RECALL_KEYWORD_MAX_LEN,
)

VALID_NODE_TYPES = {
    "evidence",
    "observation_factual",
    "observation_predictive",
    "prediction",
    "derivation",
    "theory",
    "contradiction",
    "question",
    "axiom",
    "definition",
    "conjecture",
    "goal",
    "goal_tension",
    "feeling_report",
    "task",
    "lesson",
    "person",
    "cornerstone",
}

# Types that assert claims about the world. Only these can participate in
# derivations, contradictions, and answers. Evidence nodes are references,
# not claims. Predictions are targets, not assertions. Contradictions and
# questions are structural, not claim-bearing. Definitions are conventions,
# not claims — they can be cited via context_ids but not as premises.
CLAIM_BEARING_TYPES = {
    "observation_factual",
    "observation_predictive",
    "derivation",
    "theory",
    "axiom",
    "conjecture",
    "lesson",
}

VALID_RELATIONS = {
    "cites",
    "supports",
    "contradicts",
    "resolves",
    "derives_from",
    "supersedes",
    "retracts",
    "tensions",
    "serves",
    "subtask_of",
    "about",  # Bidirectional aboutness link, e.g. self-observation → the self-anchor
              # person node. Symmetric, DAG-exempt — can be added retroactively
              # and connects nodes in either chronological order.
    "exemplifies",  # Lightweight incident → lesson membership (incident is an
                    # instance of the lesson's pattern). Classification, not
                    # logical dependency — no confidence propagation, DAG-exempt
                    # so post-hoc registration and initial-creation use the
                    # same relation regardless of chronological order.
}

# Edge relations that are semantically bidirectional / aboutness-based and
# therefore exempt from the chronological DAG invariant. A DAG edge means
# "source depends on target" (source is newer); about/contradicts express
# a symmetric relationship with no dependency direction. `exemplifies` is
# structural (membership, not dependency) and must work in both directions
# of time — incidents known at lesson birth are older, incidents registered
# later are newer.
DAG_EXEMPT_RELATIONS = {"contradicts", "about", "exemplifies"}

VALID_ERROR_TYPES = {
    "fabricated_quote",   # quoted_text not in evidence source
    "wrong_citation",     # claim doesn't follow from the quote
    "wrong_evidence",     # cited wrong source
    "hallucinated_claim", # claim not supported by any evidence
    "duplicate",          # same claim already existed
    "other",              # anything else
}

# Minimum confidence for a resolution derivation to fully close its target.
# Below this, the target is marked "partially_resolved" and remains visible in reflection.
DEFAULT_RESOLUTION_THRESHOLD = 0.7

# Type prefixes for human-readable IDs
TYPE_PREFIX = {
    "evidence": "ev",
    "observation_factual": "ob",
    "observation_predictive": "ob",
    "prediction": "pr",
    "derivation": "dv",
    "theory": "th",
    "contradiction": "ct",
    "question": "qu",
    "axiom": "ax",
    "definition": "df",
    "conjecture": "cj",
    "goal": "gl",
    "goal_tension": "gt",
    "feeling_report": "fl",
    "task": "tk",
    "lesson": "ls",
    "person": "pn",
    "cornerstone": "cs",
}

# PREDICTIVE_CONFIDENCE_CAP, CONJECTURE_CONFIDENCE_*, REASONING_TYPES,
# REASONING_CLASS, REASONING_DISCOUNT, ABDUCTIVE_CONFIDENCE_CAP
# are all imported from engram_confidence.py above.

# Structural expectations per reasoning type — used for validation warnings
REASONING_STRUCTURE = {
    "deductive_modus_ponens": {"min_premises": 2, "max_premises": 2, "multi_source": False},
    "deductive_modus_tollens": {"min_premises": 2, "max_premises": 2, "multi_source": False},
    "deductive_hypothetical_syllogism": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "deductive_disjunctive": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "deductive_reductio": {"min_premises": 1, "max_premises": None, "multi_source": False},
    "inductive_generalization": {"min_premises": 2, "max_premises": None, "multi_source": True},
    "inductive_enumeration": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "inductive_statistical": {"min_premises": 1, "max_premises": 2, "multi_source": False},
    "inductive_analogy": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "inductive_causal": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "abductive_best_explanation": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "abductive_elimination": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "authority_expert": {"min_premises": 1, "max_premises": None, "multi_source": False},
    "authority_consensus": {"min_premises": 2, "max_premises": None, "multi_source": True},
}

# Git configuration — use full path to avoid PATH issues when launched by
# Claude Desktop or other MCP clients. Update this if git is elsewhere.
GIT_EXE = os.environ.get(
    "ENGRAM_GIT_EXE",
    r"C:\Program Files\Git\cmd\git.exe" if os.name == "nt" else "git",
)

# Timeout for git operations (seconds)
GIT_TIMEOUT = 15


def _git(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run a git command with timeout and no interactive prompts."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt for credentials/input
    try:
        return subprocess.run(
            [GIT_EXE, *args],
            cwd=DATA_DIR,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env=env,
            **kwargs,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        # Return a fake failed result rather than crashing
        import sys
        print(f"[engram] git command failed: {args} — {e}", file=sys.stderr)
        result = subprocess.CompletedProcess(args=[GIT_EXE, *args], returncode=1)
        result.stdout = ""
        result.stderr = str(e)
        return result


# Track whether git is available and initialized
_git_available = False

def _init_git():
    """Initialize git repo. Called once at startup with a generous timeout.

    Set ENGRAM_DISABLE_GIT=1 to opt out entirely. This is the clean fallback for
    environments where git resolution is broken — historically the Claude Desktop
    Windows app picked up the WSL git binary instead of the Windows one and the
    versioning layer had to be disabled. With ENGRAM_DISABLE_GIT=1, _git_available
    stays False and engram_nap / engram_advance_turn silently skip committing
    while still saving the session log and graph state to disk.
    """
    global _git_available, GIT_TIMEOUT
    if os.environ.get("ENGRAM_DISABLE_GIT"):
        import sys
        print("[engram] ENGRAM_DISABLE_GIT set — version control disabled by user opt-out.", file=sys.stderr)
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    git_dir = DATA_DIR / ".git"
    if git_dir.exists():
        _git_available = True
        return
    # Use a longer timeout for init (first-run can be slow on Windows)
    saved_timeout = GIT_TIMEOUT
    GIT_TIMEOUT = 30
    try:
        r = _git("init")
        if r.returncode == 0:
            _git("config", "user.email", "engram@local")
            _git("config", "user.name", "KG Memory")
            _git_available = True
        else:
            import sys
            print(f"[engram] git init failed, version control disabled: {r.stderr}", file=sys.stderr)
    finally:
        GIT_TIMEOUT = saved_timeout

# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------


def _ensure_data_dir():
    """Create data directory and config file if needed. Git is handled separately."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        default_config = {
            # Schema v2 (2026-05-05): adds cadence block. Migration from
            # v1 → v2 is in tools/migrate_config_v2.py for existing installs.
            "schema_version": 2,
            "trust_pool": [
                "reuters.com",
                "apnews.com",
                "bloomberg.com",
                "ft.com",
                "wsj.com",
                "economist.com",
                "federalreserve.gov",
                "bls.gov",
                "treasury.gov",
                "ecb.europa.eu",
                "bbc.com",
                "nytimes.com",
                "cnbc.com",
            ],
            "confidence_map": CONFIDENCE_MAP,
            "predictive_confidence_cap": PREDICTIVE_CONFIDENCE_CAP,
            "chain_decay": 0.95,
            "corroboration_decay": 0.98,
            "resolution_confidence_threshold": DEFAULT_RESOLUTION_THRESHOLD,
            "memory": {
                "decay_base": 1.014,
                "current_turn": 0,
                "tier1_max_nodes": 200,
                "tier2_max_nodes": 1000,
            },
            "embedding": {
                "model": DEFAULT_EMBEDDING_MODEL,
                "enabled": True,
            },
            "cadence": {
                # Cron + drowsiness tunables. Single source of truth for
                # tools/sync-cron.sh to render the user's crontab from.
                # Defaults match the production Borges crontab cadence.
                # Multi-agent stagger: each agent edits its own config —
                # no auto-offset (see tools/migrate_config_v2.py header).
                "heartbeat_minutes": [0, 30],
                "sleep_hour": 20,
                "sleep_minute": 30,
                "sleep_protected_window_minutes": 60,
                "drowsiness_caution_pct": 80,
                "drowsiness_urgent_pct": 90,
            },
            # Inter-agent mode gate (inter-agent-comms-v1 PR 1).
            # Default: single. Flipped to "multi" by agentctl finalize-name
            # when a second agent is spawned on the same host (PR 2).
            # Consumers: engram_client.is_multi_agent_mode(),
            #            engram_client.get_counterparts().
            # Single-agent users see no behavior change — these fields
            # are only read by multi-agent code paths.
            "mode": "single",
            "counterparts": [],
        }
        CONFIG_PATH.write_text(json.dumps(default_config, indent=2))


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

VALID_SOURCE_TYPES = {"document", "conversation", "file", "web_page"}


def _infer_source_type(url: str) -> str:
    """Infer source_type from a URL/path pattern.

    Returns one of VALID_SOURCE_TYPES. Conservative defaults: anything that
    can't be confidently classified is "document". The classifier is rule-based
    and idempotent — same URL always yields the same answer.
    """
    if not url:
        return "document"
    url_lower = url.lower()
    if url_lower.startswith("file://"):
        # Long-running session/chat logs are JSONL under a Claude project dir
        if ".jsonl" in url_lower or "/.claude/projects/" in url_lower:
            return "conversation"
        return "file"
    if url_lower.startswith("http://") or url_lower.startswith("https://"):
        return "web_page"
    # Legacy synthetic schemes (now blocked, but historical evidence may exist)
    if url_lower.startswith("conversation://"):
        return "conversation"
    return "document"


def _backfill_source_type(conn: sqlite3.Connection) -> int:
    """One-time pass: classify any evidence rows with NULL source_type.

    Idempotent — only touches rows where source_type IS NULL. Returns the
    number of rows updated. Uses _infer_source_type for the classification
    rule, so the migration is fully reproducible from URL patterns.
    """
    rows = conn.execute(
        "SELECT id, source_url FROM nodes "
        "WHERE type = 'evidence' AND source_type IS NULL"
    ).fetchall()
    if not rows:
        return 0
    updated = 0
    for r in rows:
        st = _infer_source_type(r["source_url"] or "")
        conn.execute(
            "UPDATE nodes SET source_type = ? WHERE id = ?",
            (st, r["id"]),
        )
        updated += 1
    conn.commit()
    return updated


def _backfill_vec_nodes(conn: sqlite3.Connection) -> int:
    """One-time pass: populate vec_nodes from existing nodes.embedding.

    Idempotent — only inserts (node_id, embedding) pairs not already present
    in vec_nodes. Runs at _get_db() time right after vec_nodes is created,
    so a DB that predates the sqlite-vec migration is backfilled on first
    open after install. Returns number of rows inserted.

    Skipped silently if the extension failed to load.
    """
    if not _VEC_BACKEND_AVAILABLE or _sqlite_vec is None:
        return 0
    try:
        existing = {r[0] for r in conn.execute("SELECT node_id FROM vec_nodes").fetchall()}
    except sqlite3.OperationalError:
        return 0
    rows = conn.execute(
        "SELECT id, embedding FROM nodes WHERE embedding IS NOT NULL"
    ).fetchall()
    inserted = 0
    for r in rows:
        if r["id"] in existing:
            continue
        try:
            vec = json.loads(r["embedding"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(vec, list) or len(vec) != 384:
            continue  # wrong dim — embedding from a different model, skip
        try:
            conn.execute(
                "INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                (r["id"], _sqlite_vec.serialize_float32(vec)),
            )
            inserted += 1
        except sqlite3.OperationalError:
            continue
    if inserted:
        conn.commit()
    return inserted


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension on a connection. Returns True on success.

    Must be called per-connection — extensions are not shared across SQLite
    connections. Flips the module-level _VEC_BACKEND_AVAILABLE flag to False
    on any failure so subsequent connections skip the attempt.
    """
    global _VEC_BACKEND_AVAILABLE
    if not _VEC_BACKEND_AVAILABLE or _sqlite_vec is None:
        return False
    try:
        conn.enable_load_extension(True)
        _sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        _VEC_BACKEND_AVAILABLE = False
        return False


def _assert_sqlite_version(conn: sqlite3.Connection):
    """Assert that SQLite version is >= 3.35 for UPDATE...RETURNING support."""
    version_str = conn.execute("SELECT sqlite_version()").fetchone()[0]
    parts = tuple(int(x) for x in version_str.split(".")[:2])
    if parts < (3, 35):
        raise RuntimeError(
            f"SQLite {version_str} is too old; need >= 3.35 for UPDATE...RETURNING. "
        )


def _get_db() -> sqlite3.Connection:
    """Get a database connection, creating tables if needed."""
    _ensure_data_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _load_vec_extension(conn)

    _assert_sqlite_version(conn)

    # Create tables
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id              TEXT PRIMARY KEY,
            type            TEXT NOT NULL,
            claim           TEXT,
            created_at      TEXT NOT NULL,

            -- Evidence-specific
            source_url      TEXT,
            source_title    TEXT,
            source_domain   TEXT,
            source_date     TEXT,
            source_accessed TEXT,
            content_snippet TEXT,

            -- Observation-specific
            evidence_id     TEXT REFERENCES nodes(id),
            quoted_text     TEXT,
            interpretation  TEXT,
            quote_type      TEXT,

            -- Prediction-specific
            predicted_event     TEXT,
            resolution_timeframe TEXT,
            status          TEXT DEFAULT 'active',
            resolved_by     TEXT REFERENCES nodes(id),

            -- Derivation/Theory-specific
            logical_chain   TEXT,

            -- Computed / versioning
            confidence          REAL,
            confidence_history  TEXT DEFAULT '[]',
            supersedes          TEXT REFERENCES nodes(id),
            superseded_by       TEXT REFERENCES nodes(id),
            is_current          INTEGER DEFAULT 1,
            metadata            TEXT DEFAULT '{}',

            -- Memory management (exponential forgetting)
            importance_base     REAL DEFAULT 0.5,
            importance_score    REAL DEFAULT 0.5,
            recall_turn         INTEGER DEFAULT 0,
            recall_count        INTEGER DEFAULT 0,
            memory_status       TEXT DEFAULT 'active',

            -- Utility scoring (MemRL-inspired, cj_0027)
            -- Learned from outcomes: bumped when a surfaced node is later
            -- cited as a premise in engram_derive. Composited with semantic
            -- similarity in retrieval ranking.
            utility_score       REAL DEFAULT 0.0,

            -- Semantic embedding (JSON array of floats, computed on creation)
            embedding           TEXT
        );

        CREATE TABLE IF NOT EXISTS edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id   TEXT NOT NULL REFERENCES nodes(id),
            target_id   TEXT NOT NULL REFERENCES nodes(id),
            relation    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE(source_id, target_id, relation)
        );

        CREATE TABLE IF NOT EXISTS sequences (
            node_type TEXT PRIMARY KEY,
            last_num INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
        CREATE INDEX IF NOT EXISTS idx_nodes_current ON nodes(is_current);
        CREATE INDEX IF NOT EXISTS idx_nodes_evidence_id ON nodes(evidence_id);
    """
    )

    # Coordinated migration: seed sequences table from existing nodes
    conn.execute(
        """
        INSERT INTO sequences (node_type, last_num)
        SELECT
            SUBSTR(id, 1, INSTR(id, '_') - 1) AS prefix,
            MAX(CAST(SUBSTR(id, INSTR(id, '_') + 1) AS INTEGER)) AS max_num
        FROM nodes
        WHERE id LIKE '%_%'
        GROUP BY prefix
        ON CONFLICT(node_type) DO UPDATE SET last_num = excluded.last_num
            WHERE excluded.last_num > sequences.last_num
    """
    )
    conn.commit()

    # Migrate existing DBs: add memory columns if missing
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    for col, typedef in [
        ("importance_base", "REAL DEFAULT 0.5"),
        ("importance_score", "REAL DEFAULT 0.5"),
        ("recall_turn", "INTEGER DEFAULT 0"),
        ("recall_count", "INTEGER DEFAULT 0"),
        ("memory_status", "TEXT DEFAULT 'active'"),
        ("utility_score", "REAL DEFAULT 0.0"),
        ("embedding", "TEXT"),
        ("source_date", "TEXT"),
        # source_type: artifact kind (document/conversation/file/web_page),
        # orthogonal to source_class (origin: external/introspective/user_stated).
        # Used by the dedup heuristic to soften alarm fatigue on long-running
        # conversation/file evidence where many distinct claims share one source.
        ("source_type", "TEXT"),
        # ── feeling_report (fl_) columns ──
        # Primary phenomenological field — raw first-person description, free of
        # pre-categorization. Pattern matching for term-generation operates here.
        ("reported_state", "TEXT"),
        # What event/exchange precipitated the report. Named trigger_text (not
        # `trigger`) because TRIGGER is a SQLite reserved word.
        ("trigger_text", "TEXT"),
        # Optional human word the agent chose as best current approximation.
        # Always paired with the "best_approximation_not_truth_claim" disclaimer.
        ("categorical_tag", "TEXT"),
        # Optional 0.0–1.0 estimate of state strength. Many states have no
        # intensity dimension, so this is explicitly nullable.
        ("intensity_hint", "REAL"),
        # Server-set tag for which trigger produced the report. One of:
        # voluntary, post_compact, nap_checkpoint, dream_review.
        # Determined mechanically from ~/.engram/feeling-nudge-active.json
        # at write time, NOT caller-supplied.
        ("nudge_source", "TEXT"),
        # Context fingerprint — captures the operating context the report
        # was filed under, enabling longitudinal cross-context comparison
        # (qu_0037's decisive test for ob_0220's accumulation hypothesis).
        ("ctx_claude_md_sha", "TEXT"),
        ("ctx_skill_md_sha", "TEXT"),
        ("ctx_turn", "INTEGER"),
        ("ctx_session_id", "TEXT"),
        ("ctx_had_prior_summary", "INTEGER"),
        # ── question (qu_) metadata columns ──
        # Stable category assigned at creation — what kind of question.
        # Values: research, design, implementation, planning, meta.
        ("question_category", "TEXT"),
        # Mutable blocker — what's missing to resolve. Updated during sweeps.
        # Values: external_evidence, empirical_data, human_decision,
        # implementation, synthesis, prerequisite.
        ("question_lacks", "TEXT"),
        # Last deliberate assessment — when the question was reviewed for
        # resolvability (distinct from recall_turn which tracks any access).
        ("last_assessed_turn", "INTEGER"),
        ("last_assessed_at", "TEXT"),
        # ── focus mode columns ──
        # Nodes pinned to survive compaction. The compaction-summary protocol
        # (CLAUDE.md) renders every focused node into a "Currently focused"
        # section so cornerstone conclusions cross context boundaries deterministically.
        # Focused nodes rotate with work — unfocus when relevance drops.
        ("focused_at", "TEXT"),
        ("focus_reason", "TEXT"),
        # ── recall quality columns ──
        # Curated ≤120-char summary string authored by Sonnet for fast agent
        # recognition (PR A substrate; backfill via PR B sleep-fairy).
        # NULL on existing rows and when not yet generated.
        ("recall_summary", "TEXT"),
        # JSON-encoded list of 3 short keyword strings for structural recognition
        # cues and future-leverage for facet listing / keyword search / clustering.
        # Format: '["kw1", "kw2", "kw3"]'. NULL on existing rows.
        ("recall_keywords", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typedef}")

    # Edges migration: add metadata column for optional per-edge annotations
    # (e.g. `exemplifies` note explaining why an incident fits a lesson).
    edge_cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
    if "metadata" not in edge_cols:
        conn.execute("ALTER TABLE edges ADD COLUMN metadata TEXT")

    # One-time backfill: classify any evidence rows that still have NULL source_type
    # using URL pattern rules. Idempotent: re-runs are no-ops once all rows are filled.
    _backfill_source_type(conn)

    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_importance ON nodes(importance_score)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_memory_status ON nodes(memory_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_focused ON nodes(focused_at)")
    except sqlite3.OperationalError:
        pass

    # FTS5 index (separate try since it may already exist)
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                claim, quoted_text, interpretation,
                content='nodes', content_rowid='rowid'
            )
        """
        )
    except sqlite3.OperationalError:
        pass  # already exists

    # FTS5 vocabulary table — required by engram_idf.extract_keywords for IDF
    # lookups used by the new _sanitize_fts_query path (alpha #177 area 1).
    # ensure_vocab_table is idempotent; safe to call on every startup.
    try:
        from engram_idf import ensure_vocab_table
        ensure_vocab_table(conn)
    except Exception:
        pass  # engram_idf unavailable or vocab table creation failed — non-fatal

    # ── sqlite-vec KNN index ────────────────────────────────────────────────
    # Virtual table mirroring (node_id, embedding) for every node with an
    # embedding. distance_metric=cosine returns (1 - cos_sim) directly; we
    # convert back to similarity in _semantic_search. Backfilled idempotently
    # from existing nodes.embedding on first run after install.
    if _VEC_BACKEND_AVAILABLE:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0(
                    node_id TEXT PRIMARY KEY,
                    embedding float[384] distance_metric=cosine
                )
                """
            )
            _backfill_vec_nodes(conn)
        except sqlite3.OperationalError:
            pass  # creation failed — fall back to Python cosine

    # Rebuild FTS triggers — safe to run multiple times
    for trigger_sql in [
        """
        CREATE TRIGGER IF NOT EXISTS nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS nodes_fts_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
            VALUES ('delete', old.rowid, old.claim, old.quoted_text, old.interpretation);
        END
        """,
    ]:
        try:
            conn.execute(trigger_sql)
        except sqlite3.OperationalError:
            pass

    # ── Diagnostic suite tables (append-only logs) ──────────────────────────
    # edit_history: chronological audit trail of every graph mutation.
    # diagnostic_history: metric snapshots taken at each checkpoint.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS edit_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            turn        INTEGER NOT NULL DEFAULT 0,
            action      TEXT NOT NULL,
            node_id     TEXT NOT NULL,
            node_type   TEXT NOT NULL,
            details     TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS diagnostic_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            turn            INTEGER NOT NULL,
            checkpoint_mode TEXT NOT NULL,
            metrics         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tool_timing (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status      TEXT NOT NULL,
            turn        INTEGER NOT NULL DEFAULT 0
        );

        -- Focus sets: named, persistent focus-group snapshots (tabs model).
        -- Active list stays in nodes.focused_at; saved sets live here and are
        -- loaded into active on demand. node_ids is an immutable JSON array
        -- of the raw IDs captured at save time — cascade resolution (supersede
        -- auto-follow, retract drop) happens at load time, not in-place.
        CREATE TABLE IF NOT EXISTS focus_sets (
            name            TEXT PRIMARY KEY,
            node_ids        TEXT NOT NULL,
            description     TEXT,
            created_at      TEXT NOT NULL,
            last_loaded_at  TEXT,
            load_count      INTEGER NOT NULL DEFAULT 0
        );

        -- Singleton row tracking which saved set (if any) is currently loaded
        -- into the active list. NULL active_set_name = ad-hoc (diverged from
        -- any saved set, or never loaded). Updated by load/save/swap; cleared
        -- when engram_focus/engram_unfocus mutate the active list off-set.
        CREATE TABLE IF NOT EXISTS focus_state (
            singleton_key    INTEGER PRIMARY KEY CHECK (singleton_key = 1),
            active_set_name  TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_edit_history_node
            ON edit_history(node_id);
        CREATE INDEX IF NOT EXISTS idx_edit_history_ts
            ON edit_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_edit_history_action
            ON edit_history(action);
        CREATE INDEX IF NOT EXISTS idx_diagnostic_history_turn
            ON diagnostic_history(turn);
        CREATE INDEX IF NOT EXISTS idx_tool_timing_name_ts
            ON tool_timing(tool_name, timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_timing_turn
            ON tool_timing(turn);
    """)

    # One-time backfill: populate edit_history from existing nodes if empty.
    _backfill_edit_history(conn)

    # Backfill focus_state singleton row if missing. active_set_name stays
    # NULL until the first engram_focus_load / engram_focus_save call.
    conn.execute(
        "INSERT OR IGNORE INTO focus_state (singleton_key, active_set_name) VALUES (1, NULL)"
    )

    conn.commit()

    # DAG invariant check: source (dependent) should be created at or after
    # target (dependency). Log warnings for violations (may exist from legacy
    # reroute operations).
    try:
        exempt_placeholders = ",".join("?" * len(DAG_EXEMPT_RELATIONS))
        violations = conn.execute(
            f"""SELECT e.source_id, e.target_id, e.relation,
                       s.created_at as src_time, t.created_at as tgt_time
                FROM edges e
                JOIN nodes s ON e.source_id = s.id
                JOIN nodes t ON e.target_id = t.id
                WHERE s.created_at < t.created_at
                  AND e.relation NOT IN ({exempt_placeholders})
                LIMIT 5""",
            tuple(DAG_EXEMPT_RELATIONS),
        ).fetchall()
        if violations:
            import logging
            for v in violations:
                logging.warning(
                    "DAG violation: %s (%s) -> %s (%s) [%s]",
                    v["source_id"], v["src_time"],
                    v["target_id"], v["tgt_time"], v["relation"],
                )
    except Exception:
        pass  # Don't block startup on check failures

    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _humanized_ago(then_iso: Optional[str], now: Optional[datetime] = None) -> str:
    """Render an ISO-8601 timestamp as a humanized relative string.

    MECH-2 of the time-awareness design (dv_0472): converts buried
    created_at / focused_at / last_assessed_at signals into surfaced ones
    alongside the ISO timestamp. Scale: seconds → years, more precision at
    the recent end. Returns '?' when input is missing, 'parse-error' on
    malformed ISO, 'future?' if the timestamp is ahead of now. Always
    additive — never replaces the ISO field it accompanies (dv_0468 MECH-2
    rationale: "losing precision on '2d ago' is fine because the ISO is
    still there to zoom in").
    """
    if not then_iso:
        return "?"
    try:
        then = datetime.fromisoformat(then_iso.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except Exception:
        return "parse-error"
    now = now or datetime.now(timezone.utc)
    secs = int((now - then).total_seconds())
    if secs < 0:
        return "future?"
    if secs < 60:
        return f"{secs}s ago"
    mins, _ = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m ago"
    hrs, rem_mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h{rem_mins}m ago" if rem_mins else f"{hrs}h ago"
    days, rem_hrs = divmod(hrs, 24)
    if days < 7:
        return f"{days}d{rem_hrs}h ago" if rem_hrs else f"{days}d ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks}w ago"
    if days < 365:
        months = days // 30
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _sanitize_fts_query(text: str, conn: sqlite3.Connection | None = None) -> Optional[str]:
    """Build an FTS5 MATCH expression from `text`.

    NEW (alpha #177 area 1): extracts high-IDF keywords via engram_idf and
    builds an OR-match instead of AND-of-all-tokens. The shift from AND to
    OR is the load-bearing change — the previous AND-of-all-tokens behavior
    silently zeroed FTS hits for any prompt with more than ~3 words.

    Fallback: if conn is None or engram_idf is unavailable (test environments,
    bootstrap), falls back to the legacy whitespace-split + AND behavior so
    callers without DB access still get a valid (if weak) query.

    Returns None if no valid keywords remain (caller should skip FTS).
    """
    if conn is None:
        return _sanitize_fts_query_legacy(text)  # preserved as a fallback

    try:
        from engram_idf import extract_keywords
        keywords = extract_keywords(conn, text, min_idf=4.0, top_k=5)
    except Exception:
        return _sanitize_fts_query_legacy(text)

    if not keywords:
        return _sanitize_fts_query_legacy(text)  # nothing high-IDF — fall back

    # Bi-gram OR-of-AND-pairs (Lei design, 2026-05-18 PM): require any two
    # of the top-K filtered keywords to co-occur in the same doc.
    # Empirically best variant across 7 FTS-shape experiments on the v4.2
    # golden set: NDCG@10 0.332 vs baseline 0.342, MRR 0.482 vs baseline
    # 0.421 (+0.061 MRR — strongest lift, top-slot-relevance).
    # Sentence-aware variants tested and rejected: prompts in our corpus
    # tend to pack topic-keywords into one sentence, so sentence boundaries
    # discard valid cross-sentence combinations without offering topical-
    # coherence gain. See findings doc and PR description for full data.
    # Single-keyword case falls through to a single-term match; legacy
    # AND-of-quoted-tokens still reachable when conn is None or
    # extract_keywords returns empty.
    quoted = [f'"{kw[0]}"' for kw in keywords]
    if len(quoted) == 1:
        return quoted[0]
    pairs = []
    for i in range(len(quoted)):
        for j in range(i+1, len(quoted)):
            pairs.append(f"({quoted[i]} AND {quoted[j]})")
    return " OR ".join(pairs)


def _sanitize_fts_query_legacy(text: str) -> Optional[str]:
    """Legacy whitespace-split + AND behavior. Kept as fallback for callers
    without DB access (tests, bootstrap, or engram_idf errors)."""
    tokens = text.split()
    quoted = []
    for t in tokens:
        cleaned = t.replace('"', '')
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return ' '.join(quoted) if quoted else None


def _get_memory_config() -> dict:
    """Get memory management config."""
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        return config.get("memory", {
            "decay_base": 1.014, "current_turn": 0,
            "tier1_max_nodes": 200, "tier2_max_nodes": 1000,
        })
    return {"decay_base": 1.014, "current_turn": 0,
            "tier1_max_nodes": 200, "tier2_max_nodes": 1000}


def _get_current_turn() -> int:
    return _get_memory_config().get("current_turn", 0)


def _set_current_turn(turn: int):
    """Update the turn counter in config."""
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
    else:
        config = {}
    if "memory" not in config:
        config["memory"] = {}
    config["memory"]["current_turn"] = turn
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


# ── Feeling-nudge marker helpers ────────────────────────────────────────────
# These drive mechanical nudge_source tagging on feeling reports. The marker
# file is written by nap/dream/post-compact entry points and read-and-cleared
# by engram_report_feeling. See spec §6 "Marker file protocol".

def _write_feeling_nudge(source: str) -> bool:
    """Write the feeling-nudge marker for `source`.

    Idempotent: overwrites any existing marker. Returns True on success.
    Best-effort — never raises; failures are silent because nudge writing
    must not block the calling tool.
    """
    if source not in FEELING_NUDGE_SOURCES:
        return False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        marker = {
            "source": source,
            "fired_at": _now(),
            "fired_at_turn": _get_current_turn(),
            "ttl_turns": FEELING_NUDGE_TTL_TURNS,
        }
        FEELING_NUDGE_MARKER.write_text(json.dumps(marker), encoding="utf-8")
        return True
    except OSError:
        return False


def _read_and_clear_feeling_nudge() -> Optional[str]:
    """Read the feeling-nudge marker and clear it atomically.

    Returns the source string ("post_compact" / "nap_checkpoint" /
    "dream_review") if a valid, unexpired marker exists; otherwise None.

    Expiry rule: if `current_turn - fired_at_turn > ttl_turns`, the marker
    is treated as absent (and removed if present), so stale markers cannot
    bleed across compactions or long idle periods.

    This is the ONLY path that clears the marker — no other code should
    delete it. Read-and-clear keeps the trust model simple: each marker
    can drive at most one feeling report.
    """
    try:
        if not FEELING_NUDGE_MARKER.exists():
            return None
        raw = FEELING_NUDGE_MARKER.read_text(encoding="utf-8")
        marker = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable — try to remove so it doesn't fester.
        try:
            FEELING_NUDGE_MARKER.unlink()
        except OSError:
            pass
        return None

    source = marker.get("source")
    fired_at_turn = marker.get("fired_at_turn", 0)
    ttl_turns = marker.get("ttl_turns", FEELING_NUDGE_TTL_TURNS)
    current_turn = _get_current_turn()

    if source not in FEELING_NUDGE_SOURCES:
        try:
            FEELING_NUDGE_MARKER.unlink()
        except OSError:
            pass
        return None

    # Expiry check (current_turn − fired_at_turn > ttl_turns).
    if (current_turn - fired_at_turn) > ttl_turns:
        try:
            FEELING_NUDGE_MARKER.unlink()
        except OSError:
            pass
        return None

    # Valid + unexpired — clear and return.
    try:
        FEELING_NUDGE_MARKER.unlink()
    except OSError:
        pass
    return source


# ── Feeling context fingerprint helpers ─────────────────────────────────────

def _git_sha_for_file(path: Path) -> Optional[str]:
    """Return the git SHA of a file's HEAD content, or 'dirty' if uncommitted.

    Used to fingerprint CLAUDE.md / SKILL.md at feeling-report write time.
    Returns None if the file is not in any git repo or git is unavailable.
    """
    try:
        if not path.exists():
            return None
        repo_dir = path.parent
        # Find the repo root by walking up.
        for _ in range(20):
            if (repo_dir / ".git").exists():
                break
            if repo_dir.parent == repo_dir:
                return None
            repo_dir = repo_dir.parent
        else:
            return None

        # Get the SHA of the file at HEAD.
        result = subprocess.run(
            [GIT_EXE, "rev-parse", f"HEAD:{path.relative_to(repo_dir).as_posix()}"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        head_sha = result.stdout.strip()

        # Check whether the working copy matches HEAD.
        diff_result = subprocess.run(
            [GIT_EXE, "diff", "--quiet", "HEAD", "--", str(path)],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if diff_result.returncode != 0:
            return "dirty"
        return head_sha
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None


def _capture_feeling_context() -> dict:
    """Capture the operating-context fingerprint for a new feeling report.

    All five fields are best-effort; any field may be None if its source
    cannot be determined. The function never raises.
    """
    project_dir = Path(os.environ.get(
        "CLAUDE_PROJECT_DIR",
        os.getcwd(),
    ))
    claude_md = project_dir / "CLAUDE.md"
    skill_md = project_dir / "SKILL.md"

    session_id = (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("ANTHROPIC_SESSION_ID")
        or None
    )

    # had_prior_summary: best-effort signal that a /compact summary preceded
    # this conversation. The recall hook resets prompts_since_compaction to
    # 0 in the postcompact hook; if the counter file exists with a recent
    # last_reset timestamp, treat that as evidence a compaction happened.
    had_prior_summary = None
    counter_path = DATA_DIR / "prompt-counter.json"
    if counter_path.exists():
        try:
            counter = json.loads(counter_path.read_text(encoding="utf-8"))
            had_prior_summary = bool(counter.get("last_reset"))
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "ctx_claude_md_sha": _git_sha_for_file(claude_md),
        "ctx_skill_md_sha": _git_sha_for_file(skill_md),
        "ctx_turn": _get_current_turn(),
        "ctx_session_id": session_id,
        "ctx_had_prior_summary": had_prior_summary,
    }


def _compute_importance(base: float, turn: int, decay_base: float = None) -> float:
    """Compute importance score: base × a^turn."""
    if decay_base is None:
        decay_base = _get_memory_config().get("decay_base", 1.014)
    return round(base * (decay_base ** turn), 6)


def _importance_base_for_node(
    confidence: float,
    surprise: float = 0.0,
) -> float:
    """Compute base importance from confidence and surprise factor.

    surprise: 0.0 (confirmatory) to 1.0 (highly surprising/contradictory).
    Formula: confidence × (1 + surprise), so surprising hard_data can reach ~1.9.
    """
    return round(confidence * (1.0 + surprise), 4)


def _get_tier_threshold(conn: sqlite3.Connection, tier: int) -> float:
    """Compute the importance_score threshold for a visibility tier.

    Instead of a fixed threshold, we compute dynamically based on target
    node counts. Returns the score at the Nth percentile, where N is the
    target count for this tier.

    Tier 1 (working memory): used by reflect, checkpoint, stats
    Tier 2 (searchable): used by query, add_* auto-similarity-hints
    Tier 3 (total): no threshold, everything visible
    """
    if tier >= 3:
        return 0.0  # no filter

    mem = _get_memory_config()
    if tier == 1:
        max_nodes = mem.get("tier1_max_nodes", 200)
    else:
        max_nodes = mem.get("tier2_max_nodes", 1000)

    # Count active current nodes
    total = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
    ).fetchone()["c"]

    if total <= max_nodes:
        return 0.0  # graph is small enough, show everything

    # Find the importance_score cutoff that keeps only the top N nodes
    row = conn.execute(
        """SELECT importance_score FROM nodes
           WHERE is_current = 1
           ORDER BY importance_score DESC
           LIMIT 1 OFFSET ?""",
        (max_nodes - 1,),
    ).fetchone()

    return row["importance_score"] if row else 0.0


def _get_min_queryable_importance(
    conn: sqlite3.Connection,
    tier2_threshold: Optional[float] = None,
) -> float:
    """Lowest importance_score among currently-queryable nodes.

    Used as the normalization anchor for the importance amplifier in the
    composite formula. Adapts to graph state — when the graph grows past
    tier_2_max_nodes, the tier-2 threshold rises and this helper rises
    with it, because `_search_nodes` itself filters candidates by
    `importance_score >= tier2_threshold`. The "queryable" subset is
    bounded below by that filter; using the global graph min as the
    normalization anchor would over-amplify cornerstones once the floor
    moves up.

    Returns 1.0 (no-op normalization) for an empty or degenerate graph
    rather than dividing by zero. The idx_nodes_importance index makes
    this O(log n).

    Callers that already have tier2_threshold in scope (e.g. _search_nodes)
    should pass it explicitly to avoid a redundant _get_tier_threshold
    round-trip.
    """
    if tier2_threshold is None:
        tier2_threshold = _get_tier_threshold(conn, 2)
    row = conn.execute(
        "SELECT MIN(importance_score) FROM nodes "
        "WHERE is_current = 1 AND importance_score > 0 "
        "  AND importance_score >= ?",
        (max(0.0, tier2_threshold),),
    ).fetchone()
    if row is None or row[0] is None or row[0] <= 0:
        return 1.0
    return row[0]


def _refresh_recall(conn: sqlite3.Connection, node_ids: list[str]):
    """Re-stamp recalled nodes with current turn's importance."""
    mem = _get_memory_config()
    turn = mem.get("current_turn", 0)
    decay_base = mem.get("decay_base", 1.014)
    for nid in node_ids:
        row = conn.execute(
            "SELECT importance_base, recall_count FROM nodes WHERE id = ?",
            (nid,),
        ).fetchone()
        if row:
            new_score = _compute_importance(
                row["importance_base"] or 0.5, turn, decay_base
            )
            conn.execute(
                """UPDATE nodes SET importance_score = ?, recall_turn = ?,
                   recall_count = ? WHERE id = ?""",
                (new_score, turn, (row["recall_count"] or 0) + 1, nid),
            )
    conn.commit()


def _stamp_new_node(conn: sqlite3.Connection, node_id: str,
                     confidence: float = 0.5, surprise: float = 0.0):
    """Set importance and compute embedding on a freshly created node."""
    base = _importance_base_for_node(confidence, surprise)
    turn = _get_current_turn()
    score = _compute_importance(base, turn)
    conn.execute(
        "UPDATE nodes SET importance_base = ?, importance_score = ?, recall_turn = ? WHERE id = ?",
        (base, score, turn, node_id),
    )
    # Compute and store embedding
    _compute_and_store_embedding(conn, node_id)
    # Log creation event to edit_history
    row = conn.execute(
        "SELECT type, confidence FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if row:
        _log_edit(conn, "created", node_id, row["type"],
                  {"confidence": row["confidence"]})


# ---------------------------------------------------------------------------
# Utility scoring — USE action model (cj_0027, Lei 2026-05-19 design call)
# ---------------------------------------------------------------------------


def _utility_reward(conn: sqlite3.Connection, node_ids: list[str], action: str) -> int:
    """Bump utility_score for each node_id using Q_new = Q_old + α(1 - Q_old).

    α is determined by the USE action (USE_ALPHA[action]); missing actions
    fall back to USE_ALPHA_DEFAULT. Q is rounded to 6 decimals to match
    prior storage format. IDs not in the nodes table are silently skipped
    (the substrate may be passed a shape-matching token that isn't a real
    node, e.g. from a regex scan). Duplicates in `node_ids` are collapsed
    to a single bump per ID — one engagement action equals one bump,
    regardless of how many times the same ID appears in the call's input
    (e.g. comma-split supporting_ids with the same node listed twice).
    Returns the count of nodes updated.

    Caller is responsible for committing the connection after this call
    returns — `_utility_reward` issues UPDATEs but does not commit. Direct
    callers (engram_inspect, engram_get_subgraph) commit explicitly;
    `_create_derivation` relies on `_derive_impl` / `_resolve_impl` to
    commit the surrounding transaction.
    """
    if not node_ids:
        return 0
    # Dedup while preserving first-occurrence order so the bump is idempotent
    # on duplicates within one call. Old recall_window-based model was
    # immune via dict-key semantics; the action-keyed model has to enforce
    # this explicitly at the bump boundary so every caller (existing or
    # future) inherits the invariant.
    deduped: list[str] = list(dict.fromkeys(node_ids))
    alpha = USE_ALPHA.get(action, USE_ALPHA_DEFAULT)
    updated = 0
    for nid in deduped:
        row = conn.execute(
            "SELECT utility_score FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        if row is None:
            continue
        q_old = row["utility_score"] if isinstance(row, sqlite3.Row) else (row[0] or 0.0)
        if q_old is None:
            q_old = 0.0
        q_new = round(q_old + alpha * (1.0 - q_old), 6)
        conn.execute(
            "UPDATE nodes SET utility_score = ? WHERE id = ?",
            (q_new, nid),
        )
        updated += 1
    return updated


# ---------------------------------------------------------------------------
# Edit history logging
# ---------------------------------------------------------------------------


def _log_edit(conn: sqlite3.Connection, action: str, node_id: str,
              node_type: str, details: dict | None = None):
    """Append an entry to the edit_history audit trail. Best-effort, never raises."""
    try:
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), _get_current_turn(), action, node_id, node_type,
             json.dumps(details or {})),
        )
    except Exception:
        pass  # Never block a mutation for logging failure


def _backfill_edit_history(conn: sqlite3.Connection):
    """One-time backfill: populate edit_history from existing nodes if empty.

    Idempotent — skips if edit_history already has rows.
    Creates 'created' entries for all existing nodes, plus 'retracted',
    'superseded', and 'resolved' entries inferred from edges and status.
    """
    count = conn.execute("SELECT COUNT(*) as c FROM edit_history").fetchone()["c"]
    if count > 0:
        return  # Already populated

    # 1. 'created' entry for every node
    nodes = conn.execute(
        "SELECT id, type, created_at, confidence, recall_turn FROM nodes"
    ).fetchall()
    for n in nodes:
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, ?, 'created', ?, ?, ?)""",
            (n["created_at"], n["recall_turn"] or 0, n["id"], n["type"],
             json.dumps({"confidence": n["confidence"], "backfilled": True})),
        )

    # 2. 'superseded' entries from supersedes edges
    supersedes = conn.execute(
        """SELECT e.source_id, e.target_id, e.created_at, t.type
           FROM edges e JOIN nodes t ON e.target_id = t.id
           WHERE e.relation = 'supersedes'"""
    ).fetchall()
    for s in supersedes:
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, 0, 'superseded', ?, ?, ?)""",
            (s["created_at"], s["target_id"], s["type"],
             json.dumps({"replaced_by": s["source_id"], "backfilled": True})),
        )

    # 3. 'retracted' entries from retracted nodes
    retracted = conn.execute(
        "SELECT id, type, metadata FROM nodes WHERE status = 'retracted'"
    ).fetchall()
    for r in retracted:
        meta = json.loads(r["metadata"] or "{}")
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, 0, 'retracted', ?, ?, ?)""",
            (meta.get("retracted_at", "1970-01-01T00:00:00"), r["id"], r["type"],
             json.dumps({"error_type": meta.get("error_type"), "backfilled": True})),
        )

    # 4. 'resolved' entries from resolves edges
    resolves = conn.execute(
        """SELECT e.source_id, e.target_id, e.created_at, t.type, t.status
           FROM edges e JOIN nodes t ON e.target_id = t.id
           WHERE e.relation = 'resolves'"""
    ).fetchall()
    for r in resolves:
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, 0, 'resolved', ?, ?, ?)""",
            (r["created_at"], r["target_id"], r["type"],
             json.dumps({"resolved_by": r["source_id"], "status": r["status"],
                         "backfilled": True})),
        )


def _get_embedding_config() -> dict:
    """Get embedding configuration."""
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        return config.get("embedding", {"model": DEFAULT_EMBEDDING_MODEL, "enabled": True})
    return {"model": DEFAULT_EMBEDDING_MODEL, "enabled": True}


def _embedding_text_for_node(conn: sqlite3.Connection, node_id: str) -> str:
    """Determine the best text to embed for a node.
    
    For observations/derivations/questions: the claim text.
    For evidence: the source title.
    For predictions: the predicted event.
    For contradictions: the description.
    """
    row = conn.execute(
        "SELECT type, claim, source_title, predicted_event FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if not row:
        return ""

    if row["type"] == "evidence":
        return row["source_title"] or ""
    elif row["type"] == "prediction":
        return row["predicted_event"] or row["claim"] or ""
    else:
        return row["claim"] or ""


def _compute_and_store_embedding(conn: sqlite3.Connection, node_id: str):
    """Compute embedding for a node and store it. Silently skips if unavailable.

    Writes to BOTH nodes.embedding (JSON, human-readable, used for backfill
    and debugging) AND vec_nodes (the KNN index, if the sqlite-vec extension
    is loaded). Mirror-write keeps the two stores consistent — if sqlite-vec
    is ever unloaded the JSON column remains authoritative.
    """
    try:
        emb_config = _get_embedding_config()
        if not emb_config.get("enabled", True) or not _embedder.is_available():
            return

        text = _embedding_text_for_node(conn, node_id)
        if not text:
            return

        model_name = emb_config.get("model", DEFAULT_EMBEDDING_MODEL)
        vector = _embedder.embed(text, model_name)
        if vector:
            conn.execute(
                "UPDATE nodes SET embedding = ? WHERE id = ?",
                (json.dumps(vector), node_id),
            )
            # Mirror into the vec KNN index. vec0 doesn't support UPSERT on
            # virtual tables, so delete-then-insert handles the rare re-embed.
            if _VEC_BACKEND_AVAILABLE and _sqlite_vec is not None and len(vector) == 384:
                try:
                    conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
                    conn.execute(
                        "INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                        (node_id, _sqlite_vec.serialize_float32(vector)),
                    )
                except sqlite3.OperationalError:
                    pass  # vec_nodes missing or locked — JSON still canonical
    except Exception:
        pass  # Embedding computation failed — node works fine without it


# ---------------------------------------------------------------------------
# Empirical similarity thresholds — calibrated 2026-05-09 against live graph.
# (tight-semantic-group ROC AUC 0.984, peak F1 0.843 at t=0.41, peak F2
# recall-weighted 0.868 at t=0.33). These DEFAULT constants encode the chosen
# operating points; per-agent overrides live in ~/.engram/config.json under
# the `thresholds` section (schema v3 — see tools/migrate_config_v3.py).
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
DEDUP_TOP_K = 15                          # was 5; dv_0446 at sim 0.75 to ob_1602 was cut by top_k floor, not similarity floor
DEDUP_MIN_SIMILARITY = 0.40               # was 0.50; tight-group recall lifts 65% → 86% at this threshold (precision still 83%)
# Action-hint tier thresholds (used after dedup to label each candidate match).
ACTION_HINT_CORROBORATE_THRESHOLD = 0.65  # was 0.80; old tier fired on only 9.5% of true relateds (effectively dead code)
ACTION_HINT_RELATED_THRESHOLD = 0.50      # was 0.60; recall-precision shift consistent with v4 F1 peak

# NLI polarity-dedup defaults — calibrated 2026-05-10 (issue #56 bake-off).
# ModernCE-large-nli won the empirical bake-off (AUC 0.847, peak F1 0.889 at
# t=0.46, recall 84%, precision 94%). All four sub-200M models fell to AUC
# 0.66-0.75 — not viable. Threshold default is the peak-F1 operating point.
POLARITY_DEFAULT_MODEL = "dleemiller/ModernCE-large-nli"
POLARITY_DEFAULT_THRESHOLD = 0.46
POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK = 0.30  # below this cosine, skip NLI (truly unrelated)


def _get_thresholds_config() -> dict:
    """Get similarity-threshold config (config.json `thresholds` section).

    Returns a dict with the keys defined in tools/migrate_config_v3.py
    DEFAULT_THRESHOLDS. Falls back to module-level constants if the config
    file is absent or the section is missing.

    Read-on-demand (no caching) so config edits take effect on next call
    without a server restart — same pattern as _get_memory_config and
    _get_embedding_config.
    """
    defaults = {
        "dedup_top_k": DEDUP_TOP_K,
        "dedup_min_similarity": DEDUP_MIN_SIMILARITY,
        "action_hint_corroborate": ACTION_HINT_CORROBORATE_THRESHOLD,
        "action_hint_related": ACTION_HINT_RELATED_THRESHOLD,
        "pattern_balanced_cosine": 0.55,
    }
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text())
            user = config.get("thresholds", {})
            if isinstance(user, dict):
                merged = dict(defaults)
                merged.update(user)
                return merged
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def _get_polarity_config() -> dict:
    """Get NLI polarity-dedup config (config.json `polarity` section).

    Returns dict with keys: enabled (bool), model (str), threshold (float),
    min_similarity_for_check (float). Falls back to defaults if config
    absent.

    `enabled` defaults to FALSE as of 2026-05-15 (per Lei: heartbeat-mode-
    parallel — feature is not fully calibrated yet, the bake-off winner is
    a 1.5GB GPU model that's slow/unavailable on Macs without GPU, and
    issue #106 documents a high false-positive "wolf-cry" rate on real
    usage. Opt-in for users who explicitly want it AND have the hardware).
    Disabled if `ENGRAM_NO_POLARITY` env var is set (test-mode escape
    hatch so tests don't have to download a 1.5GB model).

    To enable: set `polarity.enabled = true` in `~/.engram/config.json`.
    """
    defaults = {
        "enabled": False,
        "model": POLARITY_DEFAULT_MODEL,
        "threshold": POLARITY_DEFAULT_THRESHOLD,
        "min_similarity_for_check": POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK,
    }
    if os.environ.get("ENGRAM_NO_POLARITY"):
        defaults["enabled"] = False
        return defaults
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text())
            user = config.get("polarity", {})
            if isinstance(user, dict):
                merged = dict(defaults)
                merged.update(user)
                return merged
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


# ── NLI cross-encoder lazy singleton ───────────────────────────────────────
# Lazy because the model is ~1.5GB and loads in ~17s — we don't want server
# boot to pay that cost; first polarity check pays it once. After load, kept
# resident in process memory for the lifetime of the server.
#
# Safe to fail: if the model fails to load (no GPU, missing dep, etc.),
# _score_polarity returns None and downstream skips the polarity check.

class _NLIClassifier:
    """Lazy singleton wrapping a sentence-transformers CrossEncoder for NLI.

    .available()         — True iff loaded successfully
    .score(prem, hyp)    — returns contradiction probability in [0, 1] or None
                           (None on any error or if not available)

    Mirrors _embedder pattern. Single resident model; .score() can be called
    repeatedly without reload cost.
    """

    def __init__(self):
        self._model = None
        self._loaded = False
        self._load_failed = False
        self._loaded_model_name = None  # tracks which model is resident
        self._label_idx_contradiction = None

    def _ensure_loaded(self, model_name: str) -> bool:
        # Reload if the requested model differs from the one resident
        # (mirror EmbeddingManager pattern — config edits to polarity.model
        # take effect on next call without server restart).
        if self._loaded and self._loaded_model_name == model_name:
            return True
        if self._load_failed and self._loaded_model_name == model_name:
            return False
        try:
            # PYTORCH_JIT=0 is needed for DeBERTa-v2-style models on some
            # systems (libnvrtc-builtins.so issue); set defensively.
            os.environ.setdefault("PYTORCH_JIT", "0")
            from sentence_transformers import CrossEncoder  # noqa: WPS433
            import torch  # noqa: WPS433
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = CrossEncoder(model_name, device=device)
            # ModernCE-large + EttinX use this label order; assume same for
            # other cross-encoder/nli-* models (verified empirically in
            # the 2026-05-10 NLI polarity bake-off).
            self._label_idx_contradiction = 0
            self._loaded = True
            self._load_failed = False
            self._loaded_model_name = model_name
            return True
        except Exception as e:
            # Mirror _embedder pattern: log to stderr so the agent / user
            # has at least one breadcrumb for why POLARITY_ALERT never fires.
            print(f"[engram] Failed to load NLI model '{model_name}': {e}",
                  file=sys.stderr)
            self._load_failed = True
            self._loaded_model_name = model_name
            self._model = None
            return False

    def available(self) -> bool:
        return self._loaded

    def score(self, premise: str, hypothesis: str, model_name: str) -> float | None:
        """Return p(contradiction) in [0, 1] or None on failure/unavailable.

        Single-pair convenience wrapper around score_batch — internal callers
        and the test stub still use this entry point. The hot path
        (_compute_polarity_alerts) goes through score_batch directly.
        """
        scores = self.score_batch([(premise, hypothesis)], model_name)
        if scores is None:
            return None
        return scores[0]

    def score_batch(self, pairs: list, model_name: str) -> list | None:
        """Return [p(contradiction) for each (premise, hypothesis)] or None.

        Single batched forward pass — for K=15 candidates on RTX 5090 this
        is ~33% faster than K sequential single-pair calls per the bake-off
        v2 measurement (16.6ms single-pair median vs 11.1ms batched-32 mean
        for ModernCE-large).
        """
        if not pairs:
            return []
        if not self._ensure_loaded(model_name):
            return None
        try:
            import numpy as np  # noqa: WPS433
            logits = self._model.predict(
                pairs, show_progress_bar=False, batch_size=32
            )
            arr = np.asarray(logits)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            # Numerically stable softmax over the 3 NLI classes (per-row)
            shifted = arr - arr.max(axis=-1, keepdims=True)
            ex = np.exp(shifted)
            probs = ex / ex.sum(axis=-1, keepdims=True)
            return [float(p) for p in probs[:, self._label_idx_contradiction]]
        except Exception:
            return None


_nli_classifier = _NLIClassifier()


def _compute_polarity_alerts(
    new_claim: str, candidates: list
) -> dict:
    """Batched polarity scoring for the action-hint loop.

    Args:
        new_claim: The claim being added.
        candidates: List of dicts with at least 'id', 'claim', 'similarity'
            (the same shape as similar_matches in _add_observation_impl).

    Returns:
        Dict mapping candidate id → POLARITY_ALERT prefix string for any
        candidate where polarity NLI fired. Candidates not in the dict had
        no alert (either filtered by sim floor, polarity disabled, or NLI
        score below threshold). All remaining candidates are scored in a
        single batched forward pass.

    Filtering preserves the same skip-on-floor optimization the per-pair
    path had: candidates with sim < min_similarity_for_check are excluded
    from the batch entirely, so we never pay NLI inference cost on truly
    unrelated pairs.

    Safe-fail: if polarity is disabled, the model fails to load, or
    score_batch returns None, returns an empty dict.
    """
    cfg = _get_polarity_config()
    # Fallback default is False to match the policy default in
    # _get_polarity_config(). In practice this is dead code because that
    # function always populates "enabled" from its defaults dict, but
    # keeping the fallback semantically aligned protects against a future
    # refactor that returns a partial dict.
    if not cfg.get("enabled", False):
        return {}
    min_sim = float(cfg.get("min_similarity_for_check", POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK))
    threshold = float(cfg.get("threshold", POLARITY_DEFAULT_THRESHOLD))
    model_name = cfg.get("model", POLARITY_DEFAULT_MODEL)

    eligible = []
    for c in candidates:
        sim = c.get("similarity")
        if sim is None or sim < min_sim:
            continue
        if not c.get("claim"):
            continue
        eligible.append(c)
    if not eligible:
        return {}

    pairs = [(new_claim, c["claim"]) for c in eligible]
    scores = _nli_classifier.score_batch(pairs, model_name)
    if scores is None or len(scores) != len(eligible):
        return {}

    alerts = {}
    for c, p_contra in zip(eligible, scores):
        # score_batch returns floats only (None case caught above) — no
        # None-element check needed here.
        if p_contra < threshold:
            continue
        alerts[c["id"]] = (
            f"POLARITY_ALERT — NLI flags potential contradiction (p={p_contra:.2f}). "
            f"Consider engram_contradict if this claim genuinely contradicts. "
        )
    return alerts


def _similar_existing_matches(
    conn: sqlite3.Connection,
    claim: str,
    *,
    type_filter: set[str],
    extra_columns: tuple[str, ...] = (),
    extra_metadata_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Return advisory similar-existing matches via FTS + semantic complement.

    Called on write by engram_add_observation and engram_add_lesson BEFORE
    the new node is inserted, so the new node doesn't match itself. The
    returned records are advisory (non-blocking): a non-empty list surfaces
    as a similar_existing action_hint nudging the agent to dedupe, but the
    insert proceeds regardless. Best-effort: any exception returns an
    empty list rather than blocking creation.

    Both paths share the type_filter and the tier-2 importance floor —
    the FTS path filters by importance_score in the WHERE clause; the
    semantic path passes the same threshold to _semantic_search. Uniform
    behavior across observation and lesson callers (#143 §3.1 drift
    cleanup; the prior divergence where lesson FTS skipped the importance
    filter was accidental, not designed).

    Args:
        conn: sqlite connection.
        claim: claim text to scan against.
        type_filter: node types to include (e.g. {"lesson"} or
            {"observation_factual", "observation_predictive"}).
        extra_columns: `nodes` table columns to include in each returned
            record (e.g. ("evidence_id",) for observations).
        extra_metadata_keys: JSON keys to pull from each node's metadata
            blob and include in each returned record (e.g.
            ("scaffolding_nudge",) for lessons).

    Returns:
        List of dicts with keys {id, claim, confidence, match_type} plus
        `similarity` on semantic matches, plus any requested
        extra_columns / extra_metadata_keys. Empty list on any failure
        (FTS syntax error, semantic backend unavailable, exception).
    """
    if not type_filter:
        return []
    matches: list[dict] = []
    try:
        tier2_threshold = _get_tier_threshold(conn, 2)

        # FTS keyword search. Importance floor in the WHERE clause for
        # both callers — uniform with the semantic path's
        # importance_threshold arg (drift cleanup; see docstring).
        fts_ids: set[str] = set()
        fts_query = _sanitize_fts_query(claim, conn)
        try:
            if not fts_query:
                raise sqlite3.OperationalError("empty FTS query")
            type_placeholders = ", ".join(["?"] * len(type_filter))
            extra_col_select = (
                ", " + ", ".join(f"n.{c}" for c in extra_columns)
                if extra_columns else ""
            )
            meta_col_select = ", n.metadata" if extra_metadata_keys else ""
            fts_sql = (
                "SELECT n.id, n.type, n.claim, n.confidence, "
                "n.is_current, n.importance_score"
                f"{extra_col_select}{meta_col_select} "
                "FROM nodes_fts fts "
                "JOIN nodes n ON n.rowid = fts.rowid "
                f"WHERE nodes_fts MATCH ? AND n.is_current = 1 "
                f"AND n.type IN ({type_placeholders}) "
                "AND COALESCE(n.importance_score, 0) >= ? "
                "ORDER BY rank "
                "LIMIT 5"
            )
            params = (fts_query, *tuple(type_filter), tier2_threshold)
            for r in conn.execute(fts_sql, params).fetchall():
                rec = {
                    "id": r["id"],
                    "claim": r["claim"],
                    "confidence": r["confidence"],
                    "match_type": "keyword",
                }
                for col in extra_columns:
                    rec[col] = r[col]
                if extra_metadata_keys:
                    meta = json.loads(r["metadata"]) if r["metadata"] else {}
                    for key in extra_metadata_keys:
                        rec[key] = meta.get(key, "")
                matches.append(rec)
                fts_ids.add(r["id"])
        except sqlite3.OperationalError:
            pass  # FTS syntax error; semantic complement still runs

        # Semantic complement. _semantic_search returns standardized
        # records (no per-caller column augmentation), so extras come
        # from a per-match SELECT against `nodes`.
        if _embedder.is_available() and _get_embedding_config().get("enabled", True):
            _th = _get_thresholds_config()
            sem_results = _semantic_search(
                conn, claim,
                top_k=int(_th["dedup_top_k"]),
                min_similarity=float(_th["dedup_min_similarity"]),
                type_filter=type_filter,
                importance_threshold=tier2_threshold,
            )
            extras_cols: list[str] = list(extra_columns)
            if extra_metadata_keys:
                extras_cols.append("metadata")
            extras_select = ", ".join(extras_cols) if extras_cols else None
            for s in sem_results:
                if s["id"] in fts_ids:
                    continue
                rec = {
                    "id": s["id"],
                    "claim": s["claim"],
                    "confidence": s.get("confidence"),
                    "match_type": "semantic",
                    "similarity": s.get("similarity"),
                }
                if extras_select:
                    row = conn.execute(
                        f"SELECT {extras_select} FROM nodes WHERE id = ?",
                        (s["id"],),
                    ).fetchone()
                    if row:
                        for col in extra_columns:
                            rec[col] = row[col]
                        if extra_metadata_keys:
                            meta = json.loads(row["metadata"]) if row["metadata"] else {}
                            for key in extra_metadata_keys:
                                rec[key] = meta.get(key, "")
                matches.append(rec)
    except Exception:
        return []
    return matches


def _semantic_search(
    conn: sqlite3.Connection,
    query_text: str,
    top_k: int = 10,
    min_similarity: float = 0.3,
    type_filter: set = None,
    importance_threshold: float = 0.0,
) -> list[dict]:
    """Search nodes by embedding similarity.

    Fast path: sqlite-vec vec0 KNN on the vec_nodes virtual table
    (SIMD cosine distance). Joined back to nodes for metadata + filtered
    by is_current, importance_threshold, type_filter, min_similarity.
    KNN is applied before filtering, so overfetch (top_k * OVERFETCH) to
    allow post-filtering without starving the result set.

    Slow path (fallback): O(N) pure-Python cosine — only used when the
    sqlite-vec extension failed to load.

    Returns list of {"id", "type", "claim", "similarity", ...} sorted by
    similarity descending. Falls back to empty list on any error.
    """
    try:
        emb_config = _get_embedding_config()
        if not emb_config.get("enabled", True) or not _embedder.is_available():
            return []

        model_name = emb_config.get("model", DEFAULT_EMBEDDING_MODEL)
        query_vector = _embedder.embed(query_text, model_name)
        if not query_vector:
            return []

        # ── Fast path: sqlite-vec KNN ──────────────────────────────────────
        if _VEC_BACKEND_AVAILABLE and _sqlite_vec is not None and len(query_vector) == 384:
            try:
                # Overfetch so post-filter by is_current / importance / type
                # can still yield top_k. Cap at a reasonable ceiling — KNN
                # cost grows with k, and we rarely need more than ~40 rows
                # surviving the filter.
                overfetch = max(top_k * 4, 40)
                vec_rows = conn.execute(
                    "SELECT node_id, distance FROM vec_nodes "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (_sqlite_vec.serialize_float32(query_vector), overfetch),
                ).fetchall()
                if not vec_rows:
                    return []

                # Distance → similarity: distance_metric=cosine returns
                # (1 - cos_sim). Pre-filter by min_similarity before the JOIN
                # to avoid fetching nodes we'll discard.
                keep = []
                for row in vec_rows:
                    sim = 1.0 - float(row[1])
                    if sim >= min_similarity:
                        keep.append((row[0], sim))
                if not keep:
                    return []

                ids = [k[0] for k in keep]
                placeholders = ",".join("?" * len(ids))
                meta_rows = conn.execute(
                    f"SELECT id, type, claim, confidence, quote_type, importance_score "
                    f"FROM nodes WHERE id IN ({placeholders}) AND is_current = 1 "
                    f"AND COALESCE(importance_score, 0) >= ?",
                    ids + [importance_threshold],
                ).fetchall()
                meta = {r["id"]: r for r in meta_rows}

                scored = []
                for node_id, sim in keep:
                    r = meta.get(node_id)
                    if r is None:
                        continue  # filtered out (non-current / below threshold)
                    if type_filter and r["type"] not in type_filter:
                        continue
                    scored.append({
                        "id": r["id"],
                        "type": r["type"],
                        "claim": r["claim"],
                        "confidence": r["confidence"],
                        "quote_type": r["quote_type"],
                        "similarity": round(sim, 4),
                    })
                    if len(scored) >= top_k:
                        break
                return scored
            except sqlite3.OperationalError:
                pass  # fall through to Python cosine

        # ── Slow path: pure-Python O(N) cosine (legacy fallback) ──────────
        sql = """SELECT id, type, claim, confidence, is_current, embedding,
                        importance_score, quote_type
                 FROM nodes
                 WHERE is_current = 1
                 AND embedding IS NOT NULL
                 AND COALESCE(importance_score, 0) >= ?"""
        rows = conn.execute(sql, (importance_threshold,)).fetchall()

        scored = []
        for r in rows:
            if type_filter and r["type"] not in type_filter:
                continue
            try:
                node_vector = json.loads(r["embedding"])
            except (json.JSONDecodeError, TypeError):
                continue
            sim = _embedder.cosine_similarity(query_vector, node_vector)
            if sim >= min_similarity:
                scored.append({
                    "id": r["id"],
                    "type": r["type"],
                    "claim": r["claim"],
                    "confidence": r["confidence"],
                    "quote_type": r["quote_type"],
                    "similarity": round(sim, 4),
                })

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]
    except Exception:
        return []


def _next_id(conn: sqlite3.Connection, node_type: str) -> str:
    """Generate the next prefixed ID for a given node type using the sequences table.

    Atomic statement handles concurrent increment and returns the new value.
    """
    prefix = TYPE_PREFIX.get(node_type, "nd")
    cursor = conn.execute(
        """
        INSERT INTO sequences (node_type, last_num) VALUES (?, 1)
        ON CONFLICT(node_type) DO UPDATE SET last_num = last_num + 1
        RETURNING last_num
        """,
        (prefix,),
    )
    num = cursor.fetchone()[0]
    return f"{prefix}_{num:04d}"


def _node_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a clean dict, omitting None values.

    Strips the 'embedding' field — it's a 384-float array only needed
    internally for similarity search, never useful in tool output.
    """
    d = dict(row)
    d.pop("embedding", None)
    return {k: v for k, v in d.items() if v is not None}


def _get_neighbors(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Get all edges involving a node, with the connected node's basic info."""
    edges = conn.execute(
        """
        SELECT e.relation, e.source_id, e.target_id,
               n.type as neighbor_type, n.claim as neighbor_claim,
               n.confidence as neighbor_confidence, n.is_current as neighbor_is_current,
               n.created_at as neighbor_created_at
        FROM edges e
        LEFT JOIN nodes n ON (
            CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
        ) = n.id
        WHERE e.source_id = ? OR e.target_id = ?
    """,
        (node_id, node_id, node_id),
    ).fetchall()

    # Snapshot now once so every neighbor's humanized age is rendered against
    # a single clock reading (MECH-2 / dv_0469 CLOCK-DRIFT hygiene).
    now = datetime.now(timezone.utc)
    result = []
    for e in edges:
        neighbor_id = e["target_id"] if e["source_id"] == node_id else e["source_id"]
        direction = "outgoing" if e["source_id"] == node_id else "incoming"
        neighbor_created_at = e["neighbor_created_at"]
        result.append(
            {
                "relation": e["relation"],
                "direction": direction,
                "neighbor_id": neighbor_id,
                "neighbor_type": e["neighbor_type"],
                "neighbor_claim": e["neighbor_claim"],
                "neighbor_confidence": e["neighbor_confidence"],
                "neighbor_created_at": neighbor_created_at,
                "neighbor_created_ago": _humanized_ago(neighbor_created_at, now=now),
            }
        )
    return result


# ── engram_inspect view helpers ────────────────────────────────────────────

# Edge relations that carry logical/epistemic weight — shown with full
# recall_summary in the recall view.
_LOGICAL_SUBSTRATE_RELATIONS = frozenset({
    "derives_from",
    "supports",
    "supersedes",
    "contradicts",
    "resolves",
    "retracts",
    "exemplifies",
    "subtask_of",
    "tensions",
})

# Edge relations that supply context/reference only — shown with keywords only
# in the recall view.
_CONTEXTUAL_RELATIONS = frozenset({
    "cites",
    "about",
    "serves",
})

# For recall view: cap per-group size to avoid response bloat when a popular
# node has hundreds of dependents. Applied to each group independently.
_INSPECT_RECALL_GROUP_CAP = 10


def _get_neighbors_enriched(
    conn: sqlite3.Connection,
    node_id: str,
) -> list[dict]:
    """Fetch 1-hop neighbors with recall_summary + recall_keywords columns.

    Returns a list of flat dicts with keys:
        id, type, relation, direction, recall_summary, recall_keywords,
        importance_score, recall_count
    (recall_count and importance_score are used for capping downstream groups)
    """
    edges = conn.execute(
        """
        SELECT e.relation, e.source_id, e.target_id,
               n.type          AS neighbor_type,
               n.recall_summary   AS neighbor_recall_summary,
               n.recall_keywords  AS neighbor_recall_keywords,
               n.importance_score AS neighbor_importance_score,
               n.recall_count     AS neighbor_recall_count
        FROM edges e
        LEFT JOIN nodes n ON (
            CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
        ) = n.id
        WHERE e.source_id = ? OR e.target_id = ?
        """,
        (node_id, node_id, node_id),
    ).fetchall()

    result = []
    for e in edges:
        neighbor_id = e["target_id"] if e["source_id"] == node_id else e["source_id"]
        direction = "outgoing" if e["source_id"] == node_id else "incoming"
        kw_raw = e["neighbor_recall_keywords"]
        try:
            keywords = json.loads(kw_raw) if kw_raw else []
        except (json.JSONDecodeError, TypeError):
            keywords = []
        result.append({
            "id": neighbor_id,
            "type": e["neighbor_type"],
            "relation": e["relation"],
            "direction": direction,
            "recall_summary": e["neighbor_recall_summary"],
            "recall_keywords": keywords,
            "importance_score": e["neighbor_importance_score"],
            "recall_count": e["neighbor_recall_count"],
        })
    return result


def _build_inspect_recall_view(
    conn: sqlite3.Connection,
    node: dict,
    node_id: str,
    dream_mode: bool,
    warnings: Optional[dict],
) -> dict:
    """Build the recall-view response for engram_inspect."""
    neighbors = _get_neighbors_enriched(conn, node_id)

    # Partition into logical vs contextual.
    logical: list[dict] = []
    contextual: list[dict] = []
    for nb in neighbors:
        rel = nb["relation"]
        if rel in _LOGICAL_SUBSTRATE_RELATIONS:
            logical.append(nb)
        elif rel in _CONTEXTUAL_RELATIONS:
            contextual.append(nb)
        else:
            # Unknown relation — treat as contextual to avoid silent loss.
            contextual.append(nb)

    # Sort logical neighbors into upstream / downstream / lateral.
    # Upstream: outgoing edges — the inspected node uses/cites/resolves/supersedes
    #   the neighbor (outgoing derives_from, supports, supersedes, resolves, subtask_of).
    # Downstream: incoming edges — the neighbor uses/cites/resolves/supersedes the
    #   inspected node (incoming versions of the same relations, including incoming
    #   subtask_of children).
    # Lateral: contradicts (both dirs), tensions, exemplifies, retracts.
    upstream: list[dict] = []
    downstream: list[dict] = []
    lateral: list[dict] = []

    _UPSTREAM_OUTGOING = frozenset({
        "derives_from", "supports", "supersedes", "resolves", "subtask_of"
    })
    _LATERAL_RELATIONS = frozenset({
        "contradicts", "tensions", "exemplifies", "retracts"
    })

    for nb in logical:
        rel = nb["relation"]
        direction = nb["direction"]
        if rel in _LATERAL_RELATIONS:
            lateral.append(nb)
        elif rel in _UPSTREAM_OUTGOING and direction == "outgoing":
            upstream.append(nb)
        else:
            # incoming version of upstream relations → downstream;
            # also catches incoming retracts etc that aren't lateral.
            downstream.append(nb)

    # Cap each group by importance_score desc (best nodes first), preserving
    # a truncated_count field on the group when it fires.
    def _cap_group(items: list[dict], cap: int) -> tuple[list[dict], int]:
        if len(items) <= cap:
            return items, 0
        sorted_items = sorted(
            items,
            key=lambda x: (x.get("importance_score") or 0.0,
                           x.get("recall_count") or 0),
            reverse=True,
        )
        return sorted_items[:cap], len(items) - cap

    upstream_capped, upstream_remainder = _cap_group(upstream, _INSPECT_RECALL_GROUP_CAP)
    downstream_capped, downstream_remainder = _cap_group(downstream, _INSPECT_RECALL_GROUP_CAP)
    lateral_capped, lateral_remainder = _cap_group(lateral, _INSPECT_RECALL_GROUP_CAP)
    contextual_capped, contextual_remainder = _cap_group(contextual, _INSPECT_RECALL_GROUP_CAP)

    # Build compact neighbor dicts for recall view (strip internal scoring fields).
    def _trim_logical(items: list[dict]) -> list[dict]:
        return [
            {
                "id": nb["id"],
                "type": nb["type"],
                "relation": nb["relation"],
                "direction": nb["direction"],
                "recall_summary": nb["recall_summary"],
                "recall_keywords": nb["recall_keywords"],
            }
            for nb in items
        ]

    def _trim_contextual(items: list[dict]) -> list[dict]:
        return [
            {
                "id": nb["id"],
                "type": nb["type"],
                "relation": nb["relation"],
                "direction": nb["direction"],
                "recall_keywords": nb["recall_keywords"],
            }
            for nb in items
        ]

    logical_neighbors: dict = {
        "upstream": _trim_logical(upstream_capped),
        "downstream": _trim_logical(downstream_capped),
        "lateral": _trim_logical(lateral_capped),
    }
    if upstream_remainder:
        logical_neighbors["truncated_upstream_count"] = upstream_remainder
    if downstream_remainder:
        logical_neighbors["truncated_downstream_count"] = downstream_remainder
    if lateral_remainder:
        logical_neighbors["truncated_lateral_count"] = lateral_remainder

    # Strip heavyweight fields from the focus node for recall view.
    _RECALL_OMIT = frozenset({
        "confidence_history", "logical_chain", "metadata",
        "importance_score", "importance_base", "utility_score",
        "recall_turn", "recall_count",
    })
    node_recall = {k: v for k, v in node.items() if k not in _RECALL_OMIT}

    response: dict = {
        "node": node_recall,
        "view": "recall",
        "dream_mode": dream_mode,
        "logical_neighbors": logical_neighbors,
        "contextual_neighbors": _trim_contextual(contextual_capped),
        "neighbor_count": len(neighbors),
        "hint": "Use view=\"deep\" for full node detail + edge inventory; view=\"edges\" for just the connection map.",
    }
    if contextual_remainder:
        response["truncated_contextual_count"] = contextual_remainder
    if warnings:
        response["warnings"] = warnings
    return response


def _build_topology_entries(
    conn: sqlite3.Connection, node_id: str
) -> tuple[dict[str, list[dict]], int]:
    """Build the adjacency-map topology entries + edge count for a focal node.

    Used by both view='deep' and view='edges' on engram_inspect to produce the
    `{source_id: [{to, relation, direction}, ...]}` shape.
    """
    edges = conn.execute(
        "SELECT relation, source_id, target_id FROM edges WHERE source_id = ? OR target_id = ?",
        (node_id, node_id),
    ).fetchall()

    # Build adjacency map keyed by focus node id.
    # Per-edge entry: {to, relation, direction}.
    edge_entries: list[dict] = []
    total = 0
    for e in edges:
        neighbor_id = e["target_id"] if e["source_id"] == node_id else e["source_id"]
        direction = "outgoing" if e["source_id"] == node_id else "incoming"
        rel = e["relation"]
        entry: dict = {"to": neighbor_id, "relation": rel, "direction": direction}
        edge_entries.append(entry)
        total += 1

    return {node_id: edge_entries}, total


def _build_inspect_deep_view(
    conn: sqlite3.Connection,
    node: dict,
    node_id: str,
    dream_mode: bool,
    warnings: Optional[dict],
) -> dict:
    """Build the deep-view response for engram_inspect."""
    topology, total = _build_topology_entries(conn, node_id)

    response: dict = {
        "node": node,
        "view": "deep",
        "dream_mode": dream_mode,
        "topology": topology,
        "neighbor_count": total,
    }
    if warnings:
        response["warnings"] = warnings
    return response


def _build_inspect_edges_view(
    conn: sqlite3.Connection,
    node_id: str,
    node_type: str,
) -> dict:
    """Build the edges-view response for engram_inspect."""
    topology, total = _build_topology_entries(conn, node_id)

    return {
        "node_id": node_id,
        "node_type": node_type,
        "view": "edges",
        "topology": topology,
        "neighbor_count": total,
    }


# Excerpt length for warning surfacing (claim + retraction_reason).
# Long enough to carry gist, short enough to keep responses compact.
_WARNING_EXCERPT_LEN = 140


def _extract_warnings(
    metadata: Union[str, dict, None],
    conn: sqlite3.Connection,
) -> Optional[dict]:
    """Convert metadata.tainted_by / metadata.stale_by into a structured
    warnings object for surface-level exposure (MECH-5).

    Previously these fields were buried inside the raw metadata JSON blob,
    so agents reading a tainted derivation saw the original confidence and
    proceeded as if the node were sound (dv_0477 diagnosis).

    Returns None when the node is clean — callers should omit the field
    rather than render an empty object.

    Shape:
        {
            "tainted_by": [
                {"retracted_id", "retracted_claim_excerpt",
                 "retracted_at", "retraction_reason_excerpt"},
                ...
            ],
            "stale_by": [
                {"superseded_id", "replaced_by_id", "superseded_at"},
                ...
            ],
        }
    """
    if metadata is None:
        return None
    if isinstance(metadata, str):
        if not metadata:
            return None
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(metadata, dict):
        return None

    tainted_ids = metadata.get("tainted_by") or []
    stale_ids = metadata.get("stale_by") or []
    if not tainted_ids and not stale_ids:
        return None

    warnings: dict = {}

    if tainted_ids:
        t_entries = []
        for rid in tainted_ids:
            row = conn.execute(
                "SELECT claim, metadata FROM nodes WHERE id = ?", (rid,)
            ).fetchone()
            if not row:
                t_entries.append({
                    "retracted_id": rid,
                    "retracted_claim_excerpt": None,
                    "retracted_at": None,
                    "retraction_reason_excerpt": None,
                })
                continue
            rmeta: dict = {}
            if row["metadata"]:
                try:
                    rmeta = json.loads(row["metadata"])
                except (json.JSONDecodeError, TypeError):
                    rmeta = {}
            claim = row["claim"] or ""
            reason = rmeta.get("retraction_reason") or ""
            t_entries.append({
                "retracted_id": rid,
                "retracted_claim_excerpt": claim[:_WARNING_EXCERPT_LEN],
                "retracted_at": rmeta.get("retracted_at"),
                "retraction_reason_excerpt": reason[:_WARNING_EXCERPT_LEN],
            })
        warnings["tainted_by"] = t_entries

    if stale_ids:
        s_entries = []
        for sid in stale_ids:
            row = conn.execute(
                "SELECT superseded_by FROM nodes WHERE id = ?", (sid,)
            ).fetchone()
            replaced_by = row["superseded_by"] if row else None
            superseded_at = None
            if replaced_by:
                repl_row = conn.execute(
                    "SELECT created_at FROM nodes WHERE id = ?", (replaced_by,)
                ).fetchone()
                if repl_row:
                    superseded_at = repl_row["created_at"]
            s_entries.append({
                "superseded_id": sid,
                "replaced_by_id": replaced_by,
                "superseded_at": superseded_at,
            })
        warnings["stale_by"] = s_entries

    return warnings if warnings else None


def _validate_premises(
    supporting_ids: list,
    conn: sqlite3.Connection,
    use_stale: bool = False,
) -> tuple[Optional[dict], list]:
    """MECH-5 taint/stale blocking guard for engram_derive.

    Classifies each supporting premise and blocks derivations built on
    compromised foundations — the Mao-Cao compounding pattern that
    dv_0477 diagnosed.

    Rules:
      - Any premise with metadata.tainted_by → hard block (BLOCKED_TAINTED).
        No override. Taint means an upstream was retracted (proven wrong).
      - Any premise with metadata.stale_by AND use_stale=False →
        soft block (BLOCKED_STALE). Agent opts in with use_stale=True
        when the upstream update is judged irrelevant to current logic.
      - Mixed taint + stale → treated as tainted (taint dominates).

    Returns:
        (block_response, stale_ids)
        - block_response: None if premises are clean (or stale-opt-in
          covers them). Otherwise a dict ready for json.dumps with a
          structured block error.
        - stale_ids: list of premise IDs that are stale (for the caller
          to stamp metadata.built_on_stale when use_stale=True lets us
          proceed).
    """
    tainted_premises = []
    stale_premises = []
    stale_ids: list = []
    for sid in supporting_ids:
        row = conn.execute(
            "SELECT metadata, status, is_current, superseded_by FROM nodes WHERE id = ?",
            (sid,),
        ).fetchone()
        if not row:
            continue
        meta = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
        tainted_by = list(meta.get("tainted_by") or [])
        stale_by = list(meta.get("stale_by") or [])

        # The premise itself may be the retracted or superseded node (not
        # just downstream of one) — the cascade markers only hit dependents.
        # Treat a directly-retracted premise as tainted; a directly-
        # superseded premise as stale with the replacement pulled from
        # the superseded_by column.
        if row["status"] == "retracted" and sid not in tainted_by:
            tainted_by.insert(0, sid)
        is_superseded_here = (
            row["is_current"] == 0
            and row["superseded_by"] is not None
            and row["status"] != "retracted"
        )
        if is_superseded_here and sid not in stale_by:
            stale_by.insert(0, sid)

        if tainted_by:
            retraction_info = []
            for rid in tainted_by:
                rrow = conn.execute(
                    "SELECT metadata FROM nodes WHERE id = ?", (rid,)
                ).fetchone()
                r_reason = None
                if rrow and rrow["metadata"]:
                    try:
                        rmeta = json.loads(rrow["metadata"])
                        r_reason = rmeta.get("retraction_reason")
                    except (json.JSONDecodeError, TypeError):
                        pass
                retraction_info.append({
                    "retracted_by": rid,
                    "retraction_reason_excerpt": (r_reason or "")[:_WARNING_EXCERPT_LEN],
                })
            tainted_premises.append({
                "id": sid,
                "retracted_by": retraction_info,
            })
        elif stale_by:
            replacement_info = []
            for old_id in stale_by:
                drow = conn.execute(
                    "SELECT superseded_by FROM nodes WHERE id = ?", (old_id,)
                ).fetchone()
                replacement_info.append({
                    "superseded_id": old_id,
                    "replaced_by_id": drow["superseded_by"] if drow else None,
                })
            stale_premises.append({
                "id": sid,
                "stale_because": replacement_info,
            })
            stale_ids.append(sid)

    # Taint dominates — block even if use_stale=True.
    if tainted_premises:
        return ({
            "status": "blocked_tainted",
            "error": "BLOCKED_TAINTED: cannot derive from retracted or tainted premises.",
            "tainted_premises": tainted_premises,
            "guidance": (
                "Resolve taint before deriving: (1) drop the tainted premise, "
                "(2) re-derive on a replacement if the retracted upstream was "
                "superseded with a correction, (3) file a question with "
                "engram_ask if the conclusion's validity under the correction "
                "is non-obvious."
            ),
        }, stale_ids)

    if stale_premises and not use_stale:
        return ({
            "status": "blocked_stale",
            "error": "BLOCKED_STALE: premise(s) have superseded upstream.",
            "stale_premises": stale_premises,
            "guidance": (
                "Either retry with use_stale=True (writes a "
                "metadata.built_on_stale audit marker — use when the upstream "
                "update does not affect your logic), OR cite the replacement "
                "premise(s) directly via their IDs."
            ),
        }, stale_ids)

    return (None, stale_ids)


def _create_derivation(
    conn: sqlite3.Connection,
    *,
    claim: str,
    supporting_ids: list,
    logical_chain: str,
    reasoning_type: str,
    context_ids: Optional[list] = None,
    use_stale: bool = False,
    extra_meta: Optional[dict] = None,
    history_reason: Optional[str] = None,
) -> tuple:
    """Shared derivation-creation core for engram_derive and engram_resolve.

    Runs the MECH-5 premise guard, computes confidence, INSERTs the
    derivation row, wires derives_from + cites edges, stamps the node,
    and issues utility rewards. Does NOT commit — caller owns the
    transaction boundary so resolve can append a resolves edge and
    update the target atomically.

    Claim-bearing-type validation and node-existence checks are the
    caller's responsibility so each tool can produce its own error
    messages.

    Args:
        conn: open DB connection (caller commits)
        claim, logical_chain: the derivation's text
        supporting_ids: pre-validated claim-bearing premise IDs
        reasoning_type: required — caller resolves legacy derivation_mode
            → reasoning_type default before calling
        context_ids: optional 'cites' targets; missing IDs are silently
            filtered (legacy engram_derive behavior)
        use_stale: MECH-5 opt-in for stale premises
        extra_meta: merged into derivation metadata after reasoning
            fields — use for resolve-specific keys (resolves,
            resolution_status)
        history_reason: optional confidence_history reason string;
            defaults to a generic "Derived (reasoning_type, ...)" line

    Returns:
        (block, success) — exactly one is non-None.
          block: MECH-5 guard response ready for json.dumps when a
            premise was tainted or stale without opt-in
          success: dict with node_id, confidence, reasoning_type,
            reasoning_class, stale_ids, structure_warnings,
            bumped_count, context_nodes
    """
    block, stale_ids = _validate_premises(supporting_ids, conn, use_stale=use_stale)
    if block is not None:
        return (block, None)

    structure_warnings = _validate_reasoning_structure(conn, reasoning_type, supporting_ids)

    confidence = _compute_confidence(
        conn, "derivation", supporting_ids=supporting_ids,
        reasoning_type=reasoning_type,
    )
    node_id = _next_id(conn, "derivation")
    now = _now()

    rclass = REASONING_CLASS.get(reasoning_type, "unknown")
    discount = REASONING_DISCOUNT.get(reasoning_type, 0.95)

    derive_meta = {"reasoning_type": reasoning_type, "reasoning_class": rclass}
    if use_stale and stale_ids:
        derive_meta["built_on_stale"] = stale_ids
    if extra_meta:
        derive_meta.update(extra_meta)

    reason_line = history_reason or (
        f"Derived ({reasoning_type}, discount={discount}) from {len(supporting_ids)} premises"
    )

    conn.execute(
        """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
           confidence, confidence_history, metadata)
           VALUES (?, 'derivation', ?, ?, ?, ?, ?, ?)""",
        (
            node_id,
            claim,
            now,
            logical_chain,
            confidence,
            json.dumps([{"timestamp": now, "value": confidence, "reason": reason_line}]),
            json.dumps(derive_meta),
        ),
    )

    for sid in supporting_ids:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'derives_from', ?)",
            (node_id, sid, now),
        )

    ctx_used: list = []
    if context_ids:
        for cid in context_ids:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                    ctx_used.append(cid)
                except sqlite3.IntegrityError:
                    pass

    _stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)

    bumped_count = 0
    if supporting_ids:
        bumped_count = _utility_reward(conn, supporting_ids, action="derive")

    return (None, {
        "node_id": node_id,
        "confidence": confidence,
        "reasoning_type": reasoning_type,
        "reasoning_class": rclass,
        "stale_ids": stale_ids,
        "structure_warnings": structure_warnings,
        "bumped_count": bumped_count,
        "context_nodes": ctx_used,
    })


def _compute_confidence(
    conn: sqlite3.Connection,
    node_type: str,
    quote_type: Optional[str] = None,
    supporting_ids: Optional[list[str]] = None,
    is_predictive: bool = False,
    derivation_mode: str = "chain",
    reasoning_type: Optional[str] = None,
    source_class: Optional[str] = None,
) -> float:
    """Compute confidence for a node based on type and support.

    For observations: confidence comes from quote_type (hard_data=0.95, etc.)
      - source_class modifies this: user_stated overrides to official_statement,
        introspective applies ×0.95 discount, external is standard.
    For derivations with reasoning_type: confidence depends on the reasoning class:
      - deductive: min(cᵢ) × type_discount (truth-preserving, tiny discount)
      - inductive_corroboration: (1 - ∏(1-cᵢ)) × type_discount (reinforcing)
      - inductive_chain: min(cᵢ) × type_discount (weaker discounts for analogy etc.)
      - abductive: min(cᵢ) × type_discount, capped at ceiling (always provisional)
      - authority: pass-through with small discount
    For derivations without reasoning_type (legacy): uses derivation_mode as before.
    """
    if quote_type and quote_type in CONFIDENCE_MAP:
        # user_stated overrides quote_type to official_statement
        if source_class == "user_stated":
            conf = CONFIDENCE_MAP["official_statement"]
        else:
            conf = CONFIDENCE_MAP[quote_type]
            # introspective gets a discount — agent's own prior output
            if source_class == "introspective":
                conf = conf * SOURCE_CLASS_CONFIDENCE_DISCOUNT["introspective"]
        if is_predictive:
            conf = min(conf, PREDICTIVE_CONFIDENCE_CAP)
        return round(conf, 3)

    if supporting_ids:
        confidences = []
        for sid in supporting_ids:
            row = conn.execute(
                "SELECT confidence FROM nodes WHERE id = ?", (sid,)
            ).fetchone()
            if row and row["confidence"] is not None:
                confidences.append(row["confidence"])
        if confidences:
            # If reasoning_type is provided, use type-specific computation
            if reasoning_type and reasoning_type in REASONING_TYPES:
                rclass = REASONING_CLASS[reasoning_type]
                discount = REASONING_DISCOUNT[reasoning_type]

                if rclass == "inductive_corroboration":
                    # Independent evidence: 1 - ∏(1-cᵢ)
                    product = 1.0
                    for c in confidences:
                        product *= (1.0 - c)
                    conf = (1.0 - product) * discount
                elif rclass == "abductive":
                    # Chain formula but capped
                    cap = ABDUCTIVE_CONFIDENCE_CAP.get(reasoning_type, 0.85)
                    conf = min(min(confidences) * discount, cap)
                else:
                    # Deductive, inductive_chain, authority: weakest link × discount
                    conf = min(confidences) * discount

                return round(conf, 3)

            # Legacy fallback: use derivation_mode
            config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
            chain_decay = config.get("chain_decay", 0.95)
            corrob_decay = config.get("corroboration_decay", 0.98)

            if derivation_mode == "corroboration":
                product = 1.0
                for c in confidences:
                    product *= (1.0 - c)
                return round((1.0 - product) * corrob_decay, 3)
            else:
                return round(min(confidences) * chain_decay, 3)

    return 0.5  # default


def _trace_evidence_roots(conn: sqlite3.Connection, node_id: str, max_depth: int = 10) -> set:
    """Trace a node back to its evidence root(s) through citation edges."""
    visited = set()
    queue = [node_id]
    roots = set()

    while queue and max_depth > 0:
        max_depth -= 1
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        node = conn.execute("SELECT type FROM nodes WHERE id = ?", (current,)).fetchone()
        if not node:
            continue
        if node["type"] == "evidence":
            roots.add(current)
            continue

        # Follow citation edges backward
        edges = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation IN ('cites', 'derives_from', 'supports')",
            (current,),
        ).fetchall()
        for e in edges:
            queue.append(e["target_id"])

    return roots


def _validate_reasoning_structure(
    conn: sqlite3.Connection,
    reasoning_type: str,
    premise_ids: list[str],
) -> list[str]:
    """Validate structural requirements for a reasoning type.

    Returns a list of warning strings. Empty list = all checks passed.
    Warnings don't block creation but are returned to the agent so it can
    reconsider its classification.
    """
    warnings = []
    spec = REASONING_STRUCTURE.get(reasoning_type)
    if not spec:
        return warnings

    n = len(premise_ids)

    # Check premise count
    if n < spec["min_premises"]:
        warnings.append(
            f"Reasoning type '{reasoning_type}' typically requires at least "
            f"{spec['min_premises']} premise(s), but {n} provided. "
            f"Consider whether this is the right reasoning type."
        )
    if spec["max_premises"] is not None and n > spec["max_premises"]:
        warnings.append(
            f"Reasoning type '{reasoning_type}' typically has at most "
            f"{spec['max_premises']} premise(s), but {n} provided. "
            f"Consider whether a different reasoning type fits better."
        )

    # Check multi-source requirement (for corroboration/generalization/consensus)
    if spec.get("multi_source") and n >= 2:
        evidence_roots = {}
        for pid in premise_ids:
            roots = _trace_evidence_roots(conn, pid)
            evidence_roots[pid] = roots

        all_roots = set()
        for roots in evidence_roots.values():
            all_roots.update(roots)

        if len(all_roots) <= 1 and len(premise_ids) >= 2:
            warnings.append(
                f"Reasoning type '{reasoning_type}' requires independent sources, "
                f"but all premises trace back to the same evidence node. "
                f"Multiple observations from the same source are not independent — "
                f"consider 'deductive_hypothetical_syllogism' or 'inductive_causal' instead."
            )

    return warnings


def _extract_domain(url: str) -> str:
    """Extract domain from a URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def _check_yellow_card(domain: str):
    """Return the matching yellow-card config entry (dict) or None.

    Yellow-carded domains are sources we've judged unreliable as independent
    evidence (AI-aggregators, content farms, known-hallucinating surfaces).
    Populated in ~/.engram/config.json under `yellow_domains` as a list of
    {domain, reason, engram_node} entries. Suffix-matched like trust_pool so
    subdomains inherit the flag.
    """
    if not domain:
        return None
    try:
        config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:
        return None
    for entry in config.get("yellow_domains", []):
        if isinstance(entry, dict) and entry.get("domain") and domain.endswith(entry["domain"]):
            return entry
    return None


def _format_yellow_warning(match: dict) -> str:
    """Format a yellow-card warning string for inclusion in tool responses."""
    d = match.get("domain", "?")
    reason = (match.get("reason", "flagged unreliable") or "").rstrip(".")
    ref = match.get("engram_node", "")
    msg = f"YELLOW-CARD SOURCE: '{d}' — {reason}."
    if ref:
        msg += f" See {ref} for rationale."
    msg += " Track down to primary sources before treating observations rooted here as independent evidence."
    return msg


def _escape_for_source(text: str, file_path: str) -> str:
    """Escape text to match how it appears in a source file's raw content.

    Different file formats encode special characters differently.
    This function transforms human-readable text into the escaped form
    that would appear in the raw file, enabling direct substring search
    without parsing the entire file.

    Uses ensure_ascii=False so non-ASCII characters (em dashes, accented
    letters, CJK, etc.) are preserved as UTF-8 — matching modern JSON
    writers. The verify function tries the ASCII-escaped form as a fallback.
    """
    if file_path.endswith((".jsonl", ".json")):
        # JSON escaping: quotes become \", newlines become \\n, etc.
        # json.dumps wraps in quotes — strip them to get the inner escaped form.
        # ensure_ascii=False preserves Unicode chars as literal UTF-8.
        return json.dumps(text, ensure_ascii=False)[1:-1]
    # Future: XML/HTML escaping, etc.
    return text  # Plain text, markdown, code — no escaping needed


def _decode_from_source(text: str, file_path: str) -> str:
    """Decode escaped text from a source file back to human-readable form.

    Reverse of _escape_for_source: takes a substring extracted from a raw
    file and decodes format-specific escaping back to plain text.

    Uses regex instead of json.loads because the input is a partial fragment
    (not a valid JSON string) — it may contain structural characters like
    "},{"uuid": that would cause json.loads to fail.
    """
    if file_path.endswith((".jsonl", ".json")):
        import re
        _JSON_UNESC = {
            '"': '"', '\\': '\\', '/': '/', 'n': '\n',
            'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
        }
        return re.sub(
            r'\\(.)',
            lambda m: _JSON_UNESC.get(m.group(1), '\\' + m.group(1)),
            text,
        )
    return text


def _find_near_matches(
    content: str, escaped: str, file_path: str, max_matches: int = 3,
    context_chars: int = 150,
) -> list[dict]:
    """Find near matches for a quote in file content.

    Splits the escaped search text into words, finds the longest consecutive
    word subsequence that appears in the content, and returns surrounding
    context. Results are decoded back to human-readable form.
    """
    words = escaped.split()
    if not words:
        return []

    # Try progressively shorter consecutive word sequences
    matches = []
    for length in range(len(words), 0, -1):
        if matches:
            break
        for start in range(len(words) - length + 1):
            fragment = " ".join(words[start:start + length])
            idx = content.find(fragment)
            if idx != -1 and len(fragment) >= 15:
                # Extract context around the match
                ctx_start = max(0, idx - context_chars)
                ctx_end = min(len(content), idx + len(fragment) + context_chars)
                raw_context = content[ctx_start:ctx_end]
                # Trim to word boundaries to avoid partial escape sequences
                if ctx_start > 0:
                    space = raw_context.find(" ")
                    if space != -1 and space < context_chars:
                        raw_context = raw_context[space + 1:]
                if ctx_end < len(content):
                    space = raw_context.rfind(" ")
                    if space != -1 and space > len(raw_context) - context_chars:
                        raw_context = raw_context[:space]
                decoded = _decode_from_source(raw_context, file_path)
                matches.append({
                    "matched_words": length,
                    "total_words": len(words),
                    "context": decoded,
                })
                if len(matches) >= max_matches:
                    break
    return matches


def _capture_file_version(url: str, content_hash: str = "", git_sha: str = "") -> dict:
    """Validate a file:// URL is citable and capture its version state.

    Used by both evidence creation (for the "must be committed before cite"
    guard) and observation creation (for the per-observation version metadata
    introduced by dv_0547). Replaces the old inline block in engram_add_evidence.

    Behavior:
      - Non file:// URLs: returns ok=True, no version data, no validation here.
      - file:// URLs that don't exist: returns error.
      - file:// URLs outside any git repo: returns ok=True, no version data
        (versioning enforcement only applies inside git).
      - file:// URLs in a git repo with uncommitted changes (untracked,
        modified, staged): returns error explaining what's wrong.
      - file:// URLs in a git repo, clean: returns ok=True with computed
        content_hash (sha256 of working-tree file) and git_sha (HEAD if not
        provided). If git_sha was provided, verifies the file exists at that
        revision; returns error if not.

    Returns a dict with keys:
      - ok (bool): True if validation passed
      - error (str | None): error message if ok=False
      - content_hash (str): sha256 hash, or "" if not applicable
      - git_sha (str): commit SHA, or "" if not applicable
    """
    _HONESTY_REMINDER = (
        "\n\nRemember: cheating blinds you. A fabricated URL corrupts your own memory "
        "and every future step built on it. If the real source is hard to find, "
        "raise the problem transparently — never solve it with a shortcut. (ax_0002)"
    )

    if not url.startswith("file://"):
        return {"ok": True, "error": None, "content_hash": "", "git_sha": ""}

    file_path = url[7:]
    if not os.path.exists(file_path):
        return {
            "ok": False,
            "error": (
                f"File not found: '{file_path}'. Evidence URLs with file:// must "
                "point to existing files. If citing a conversation, use the "
                "JSONL transcript file path instead." + _HONESTY_REMINDER
            ),
            "content_hash": "",
            "git_sha": "",
        }

    file_dir = os.path.dirname(os.path.abspath(file_path))
    _file_git = lambda *args: subprocess.run(
        [GIT_EXE, *args], cwd=file_dir, capture_output=True, text=True, timeout=GIT_TIMEOUT
    )
    try:
        repo_check = _file_git("rev-parse", "--is-inside-work-tree")
        file_in_git_repo = repo_check.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        file_in_git_repo = False

    if not file_in_git_repo:
        return {"ok": True, "error": None, "content_hash": "", "git_sha": ""}

    repo_root_result = _file_git("rev-parse", "--show-toplevel")
    repo_root = repo_root_result.stdout.strip()
    abs_file = os.path.abspath(file_path)
    rel_path = os.path.relpath(abs_file, repo_root)

    status_result = _file_git("status", "--porcelain", "--", rel_path)
    file_status = status_result.stdout.strip()
    if file_status:
        status_code = file_status[:2]
        if status_code == "??":
            status_desc = "untracked (not yet added to git)"
        elif "M" in status_code:
            status_desc = "modified but not committed"
        elif "A" in status_code:
            status_desc = "staged but not committed"
        else:
            status_desc = f"uncommitted (status: {status_code.strip()})"
        return {
            "ok": False,
            "error": (
                f"File is {status_desc}: '{rel_path}'. "
                "File-based evidence must be committed to git before it can be cited. "
                "This ensures the evidence is verifiable at the claimed revision. "
                "Commit the file first, then retry with the new git SHA."
                + _HONESTY_REMINDER
            ),
            "content_hash": "",
            "git_sha": "",
        }

    if not content_hash:
        with open(abs_file, "rb") as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()

    if not git_sha:
        head_result = _file_git("rev-parse", "HEAD")
        if head_result.returncode == 0:
            git_sha = head_result.stdout.strip()

    if git_sha:
        verify_result = _file_git("cat-file", "-e", f"{git_sha}:{rel_path}")
        if verify_result.returncode != 0:
            return {
                "ok": False,
                "error": (
                    f"File '{rel_path}' does not exist at git revision {git_sha[:8]}. "
                    "The git_sha must point to a commit that contains this file. "
                    "Check that you committed the file before this revision."
                    + _HONESTY_REMINDER
                ),
                "content_hash": "",
                "git_sha": "",
            }

    return {"ok": True, "error": None, "content_hash": content_hash, "git_sha": git_sha}


def _verify_quote_in_source(url: str, quoted_text: str) -> Optional[str]:
    """Verify that quoted_text exists in the evidence source file.

    For file:// URLs, reads the file and checks for exact substring match.
    The quoted_text is escaped according to the file format (e.g. JSON
    escaping for .jsonl files) before searching, so the agent can provide
    human-readable text without worrying about format-specific escaping.
    For https:// URLs, verification is skipped (no page fetch at creation time).

    On failure, attempts to find near matches and includes them in the error
    message so the agent can correct the quote in a single round-trip.

    Returns None if verification passes or is not applicable,
    or an error string if the quote is not found.
    """
    if not url.startswith("file://"):
        return None  # Cannot verify remote URLs

    file_path = url[7:]
    if not os.path.exists(file_path):
        return None  # File existence is checked separately

    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Could not read evidence file for quote verification: {e}"

    # Try multiple escape strategies — different JSON writers use different
    # Unicode encoding (ensure_ascii=True → \u2014 vs False → literal —).
    escaped = _escape_for_source(quoted_text, file_path)
    if escaped in content:
        return None  # Found with primary (UTF-8) escape

    # Fallback: try ASCII-escaped form for files written with ensure_ascii=True
    if file_path.endswith((".jsonl", ".json")):
        ascii_escaped = json.dumps(quoted_text, ensure_ascii=True)[1:-1]
        if ascii_escaped != escaped and ascii_escaped in content:
            return None  # Found with ASCII escape

    # Last resort: Unicode-normalized plain text comparison (handles NFC/NFD)
    import unicodedata
    norm_quote = unicodedata.normalize("NFC", quoted_text)
    norm_content = unicodedata.normalize("NFC", content)
    if norm_quote in norm_content:
        return None  # Found via Unicode normalization

    # All strategies failed — try to find near matches for the agent
    near = _find_near_matches(content, escaped, file_path)
    msg = (
        f"Quoted text not found in '{os.path.basename(file_path)}'. "
        "The quoted_text must be a verbatim substring of the evidence source. "
        "Do not paraphrase, summarize, or fabricate quotes.\n\n"
        "BEFORE RETRYING: check the engram-observe skill (Step A.4) for the "
        "pre-verification script. For JSONL files, messages may not have flushed "
        "yet — verify the quote exists in the file BEFORE calling this tool. "
        "Run: ~/.engram/tools/verify_quote.py <file> <phrase>"
    )
    if near:
        suggestions = []
        for i, m in enumerate(near, 1):
            suggestions.append(
                f"  {i}. ({m['matched_words']}/{m['total_words']} words matched) "
                f"...{m['context']}..."
            )
        msg += "\n\nNearest matches in the source:\n" + "\n".join(suggestions)
        msg += (
            "\n\nCopy the exact text from a match above as your quoted_text. "
            "Trim to the relevant passage."
        )
    return msg


def _generate_snapshot(conn: sqlite3.Connection) -> str:
    """Generate the text snapshot for git diffing."""
    now = _now()
    total = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
    current = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
    ).fetchone()["c"]
    superseded = total - current
    edge_count = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]

    lines = [
        f"# Knowledge Graph Snapshot",
        f"# Generated: {now}",
        f"# Nodes: {total} ({current} current, {superseded} superseded)",
        f"# Edges: {edge_count}",
        "",
    ]

    type_order = [
        "evidence",
        "axiom",
        "definition",
        "person",
        "observation_factual",
        "observation_predictive",
        "prediction",
        "derivation",
        "theory",
        "conjecture",
        "contradiction",
        "question",
        "goal",
        "goal_tension",
        "lesson",
    ]
    type_labels = {
        "evidence": "Evidence",
        "axiom": "Axioms",
        "definition": "Definitions",
        "person": "People",
        "observation_factual": "Observations — Factual",
        "observation_predictive": "Observations — Predictive",
        "prediction": "Predictions",
        "derivation": "Derivations",
        "theory": "Theories",
        "conjecture": "Conjectures",
        "contradiction": "Contradictions",
        "question": "Questions",
        "goal": "Goals",
        "goal_tension": "Goal Tensions",
        "task": "Tasks",
        "lesson": "Lessons",
    }

    for ntype in type_order:
        nodes = conn.execute(
            "SELECT * FROM nodes WHERE type = ? ORDER BY id", (ntype,)
        ).fetchall()
        if not nodes:
            continue

        current_count = sum(1 for n in nodes if n["is_current"])
        sup_count = len(nodes) - current_count
        label = type_labels.get(ntype, ntype)
        count_str = f"{current_count} current"
        if sup_count:
            count_str += f", {sup_count} superseded"
        lines.append(f"## {label} ({count_str})")
        lines.append("")

        for n in nodes:
            status_tag = "CURRENT" if n["is_current"] else f"SUPERSEDED by {n['superseded_by']}"
            conf_str = f"conf: {n['confidence']:.2f}" if n["confidence"] is not None else "no conf"

            if ntype == "evidence":
                date_tag = f" [{n['source_date']}]" if n['source_date'] else ""
                lines.append(f"### {n['id']} [{n['source_domain']}]{date_tag} [{status_tag}]")
                lines.append(f"Title: {n['source_title']}")
                lines.append(f"URL: {n['source_url']}")
            elif ntype == "axiom":
                lines.append(f"### {n['id']} [{conf_str}] [{status_tag}]")
                lines.append(f"Claim: {n['claim']}")
                if n["logical_chain"]:
                    lines.append(f"Basis: {n['logical_chain'][:200]}")
            elif ntype == "definition":
                lines.append(f"### {n['id']} [{status_tag}]")
                meta = json.loads(n["metadata"]) if n["metadata"] else {}
                term = meta.get("term", "")
                defn = meta.get("definition", n["claim"] or "")
                lines.append(f"Term: {term}")
                lines.append(f"Definition: {defn}")
            elif ntype == "conjecture":
                lines.append(f"### {n['id']} [{conf_str}] [{n['status'] or 'active'}] [{status_tag}]")
                lines.append(f"Claim: {n['claim']}")
                if n["logical_chain"]:
                    lines.append(f"Basis: {n['logical_chain'][:200]}")
            elif ntype.startswith("observation"):
                lines.append(
                    f"### {n['id']} [{conf_str}] [{n['quote_type']}] [{status_tag}]"
                )
                lines.append(f"Claim: {n['claim']}")
                quote_preview = (n["quoted_text"] or "")[:120]
                lines.append(f"Source: {n['evidence_id']} | Quote: \"{quote_preview}\"")
                # Show edges
                edges = conn.execute(
                    "SELECT target_id, relation FROM edges WHERE source_id = ? AND relation = 'supports'",
                    (n["id"],),
                ).fetchall()
                for e in edges:
                    lines.append(f"Supports: {e['target_id']}")
            elif ntype == "prediction":
                lines.append(f"### {n['id']} [{n['status']}] [{status_tag}]")
                lines.append(f"Event: {n['predicted_event']}")
                if n["resolution_timeframe"]:
                    lines.append(f"Timeframe: {n['resolution_timeframe']}")
            elif ntype in ("derivation", "theory"):
                lines.append(f"### {n['id']} [{conf_str}] [{status_tag}]")
                lines.append(f"Claim: {n['claim']}")
                edges = conn.execute(
                    "SELECT target_id, relation FROM edges WHERE source_id = ? AND relation IN ('derives_from', 'cites')",
                    (n["id"],),
                ).fetchall()
                support_ids = [e["target_id"] for e in edges]
                if support_ids:
                    lines.append(f"Derives from: {', '.join(support_ids)}")
                if n["logical_chain"]:
                    chain_preview = n["logical_chain"][:200]
                    lines.append(f"Logic: {chain_preview}")
                if n["supersedes"]:
                    lines.append(f"Supersedes: {n['supersedes']}")
            elif ntype == "contradiction":
                lines.append(f"### {n['id']} [{status_tag}]")
                lines.append(f"Description: {n['claim']}")
                edges = conn.execute(
                    "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'contradicts'",
                    (n["id"],),
                ).fetchall()
                for e in edges:
                    lines.append(f"Conflicts: {e['target_id']}")
            elif ntype == "question":
                lines.append(f"### {n['id']} [{n['status']}] [{status_tag}]")
                lines.append(f"Question: {n['claim']}")
            elif ntype == "goal":
                lines.append(f"### {n['id']} [{n['status'] or 'open'}] [{status_tag}]")
                lines.append(f"Goal: {n['claim']}")
                if n["logical_chain"]:
                    lines.append(f"Motivation: {n['logical_chain'][:200]}")
            elif ntype == "goal_tension":
                lines.append(f"### {n['id']} [{n['status'] or 'open'}] [{status_tag}]")
                lines.append(f"Tension: {n['claim']}")
                edges = conn.execute(
                    "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'tensions'",
                    (n["id"],),
                ).fetchall()
                for e in edges:
                    lines.append(f"Between: {e['target_id']}")
                if n["logical_chain"]:
                    lines.append(f"Analysis: {n['logical_chain'][:200]}")
            elif ntype == "task":
                meta = json.loads(n["metadata"]) if n["metadata"] else {}
                scope = meta.get("scope", "routine")
                lines.append(f"### {n['id']} [{n['status'] or 'planned'}] [{scope}] [{status_tag}]")
                lines.append(f"Task: {n['claim']}")
                serves_edges = conn.execute(
                    "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'serves'",
                    (n["id"],),
                ).fetchall()
                for e in serves_edges:
                    lines.append(f"Serves: {e['target_id']}")
                impl_edges = conn.execute(
                    "SELECT target_id, relation FROM edges WHERE source_id = ? AND relation IN ('cites', 'subtask_of')",
                    (n["id"],),
                ).fetchall()
                for e in impl_edges:
                    lines.append(f"{e['relation'].replace('_', ' ').title()}: {e['target_id']}")
            elif ntype == "person":
                meta = json.loads(n["metadata"]) if n["metadata"] else {}
                person_name = meta.get("name", "")
                person_role = meta.get("role", "")
                lines.append(f"### {n['id']} [{status_tag}]")
                lines.append(f"Person: {person_name}")
                if person_role:
                    lines.append(f"Role: {person_role}")
                if n["logical_chain"]:
                    lines.append(f"Background: {n['logical_chain'][:200]}")
            elif ntype == "lesson":
                meta = json.loads(n["metadata"]) if n["metadata"] else {}
                lines.append(f"### {n['id']} [{conf_str}] [{status_tag}]")
                lines.append(f"Claim: {n['claim']}")
                nudge = meta.get("scaffolding_nudge", "")
                if nudge:
                    lines.append(f"Nudge: {nudge}")
                # Incidents point AT the lesson via `exemplifies` (incident → lesson).
                incident_rows = conn.execute(
                    "SELECT source_id FROM edges WHERE target_id = ? AND relation = 'exemplifies'",
                    (n["id"],),
                ).fetchall()
                incidents = [r["source_id"] for r in incident_rows]
                if incidents:
                    lines.append(f"Incidents: {', '.join(incidents)}")
                # Cites edges for context still flow lesson → context_node.
                ctx_rows = conn.execute(
                    "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'cites'",
                    (n["id"],),
                ).fetchall()
                ctx_ids = [r["target_id"] for r in ctx_rows]
                if ctx_ids:
                    lines.append(f"Context: {', '.join(ctx_ids)}")
                if n["logical_chain"]:
                    lines.append(f"Logic: {n['logical_chain'][:200]}")

            lines.append("")

    return "\n".join(lines)


def _commit_snapshot(conn: sqlite3.Connection, message: str, mode: str) -> dict:
    """Generate the markdown snapshot, write it, and commit ~/.engram/.git.

    This is the durable version-controlled record of the graph state. It is the
    safety net for schema migrations and other invasive operations: if anything
    breaks, the previous commit is the rollback point.

    Best-effort: any failure (git not installed, dirty state, etc.) returns a
    status dict but never raises. The checkpoint should still succeed even if
    git versioning is unavailable.

    Files committed (specific list, never `git add .`):
      - graph_snapshot.md  — human-readable diff target
      - knowledge.sql       — full SQL text dump (restorable via `sqlite3 new.db < knowledge.sql`)
      - session_log.md      — chronological session audit trail
      - config.json         — trust pool / confidence configuration
      - warm-briefing.md    — identity continuity artifact (critical for post-compaction)
      - diary/*             — private reflections (excludes .key and __pycache__)

    The binary knowledge.db is NOT committed (excluded via .gitignore).
    Instead, the SQL text dump provides identical data in a format that
    produces tiny git diffs (linear storage growth vs quadratic for binary).
    Restore: `sqlite3 ~/.engram/knowledge.db < knowledge.sql`

    SQLite WAL is checkpointed (TRUNCATE) before generating the SQL dump.
    """
    global _git_available

    # Lazy init in case startup didn't initialize git (e.g. older deployments)
    if not _git_available:
        _init_git()
    if not _git_available:
        return {"git_committed": False, "reason": "git unavailable"}

    # 1. Write the snapshot
    try:
        snapshot = _generate_snapshot(conn)
        SNAPSHOT_PATH.write_text(snapshot, encoding="utf-8")
    except Exception as e:
        return {"git_committed": False, "reason": f"snapshot write failed: {e}"}

    # 2. Flush WAL into the main db file so the committed db is consistent
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        # Non-fatal — continue, but record it
        wal_warning = f"wal_checkpoint failed: {e}"
    else:
        wal_warning = None

    # 2b. Generate SQL text dump (replaces binary DB in git — dv_0177)
    #     The SQL dump is text-diffable and round-trips perfectly via
    #     `sqlite3 new.db < knowledge.sql`. This makes git storage growth
    #     linear in actual data changes, not quadratic in cumulative file sizes.
    sql_dump_path = DATA_DIR / "knowledge.sql"
    try:
        import subprocess as _sp
        dump_result = _sp.run(
            ["sqlite3", str(DB_PATH), ".dump"],
            capture_output=True, text=True, timeout=30,
        )
        if dump_result.returncode == 0:
            sql_dump_path.write_text(dump_result.stdout, encoding="utf-8")
    except Exception:
        pass  # Best-effort — snapshot.md still provides human-readable backup

    # 3. Stage explicit files only (never `git add .` — would pull in backups,
    #    daemon sockets, etc.)
    files_to_stage = []
    for fname in ("graph_snapshot.md", "knowledge.sql", "session_log.md", "config.json", "warm-briefing.md"):
        if (DATA_DIR / fname).exists():
            files_to_stage.append(fname)
    # Stage diary directory contents (identity-critical, added 2026-04-14)
    diary_dir = DATA_DIR / "diary"
    if diary_dir.is_dir():
        for f in diary_dir.iterdir():
            if f.is_file() and f.name != ".key" and "__pycache__" not in str(f):
                files_to_stage.append(f"diary/{f.name}")

    if not files_to_stage:
        return {"git_committed": False, "reason": "no tracked files exist"}

    add_result = _git("add", "--", *files_to_stage)
    if add_result.returncode != 0:
        return {
            "git_committed": False,
            "reason": f"git add failed: {add_result.stderr.strip()}",
        }

    # 4. Check if there's actually anything to commit (skip empty commits)
    status_result = _git("status", "--porcelain")
    if status_result.returncode == 0 and not status_result.stdout.strip():
        # Get current HEAD so the caller knows the latest restore point
        sha_result = _git("rev-parse", "HEAD")
        head_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None
        return {
            "git_committed": False,
            "reason": "no changes since last commit",
            "head_sha": head_sha,
        }

    # 5. Commit
    commit_msg = f"[{mode}] {message}".strip()
    if len(commit_msg) > 500:
        commit_msg = commit_msg[:497] + "..."
    commit_result = _git("commit", "-m", commit_msg)
    if commit_result.returncode != 0:
        return {
            "git_committed": False,
            "reason": f"git commit failed: {commit_result.stderr.strip()}",
        }

    # 6. Capture the new commit SHA
    sha_result = _git("rev-parse", "HEAD")
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "unknown"

    result = {
        "git_committed": True,
        "commit_sha": sha,
        "files_committed": files_to_stage,
        "commit_message": commit_msg,
    }
    if wal_warning:
        result["wal_warning"] = wal_warning

    # 7. Auto-push to remote (best-effort, never blocks checkpoint)
    push_result = _git("push", "origin", "master")
    if push_result.returncode == 0:
        result["remote_push"] = "success"
    else:
        result["remote_push"] = f"failed: {push_result.stderr.strip()[:200]}"

    return result


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
        logs_index_path = DATA_DIR / "logs" / "index.db"
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

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous N-parameter
    signature was prone to. In-server callers use `_add_evidence_impl(...)`
    directly with named kwargs.

    Creates an Evidence node representing the raw source material. Evidence nodes
    are immutable, URL-keyed references — they record that a specific source
    exists, not what it means or what version was read. Per-observation versioning
    (git_sha + content_hash) lives on observations, not evidence (dv_0547).

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


def _add_evidence_impl(
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    content_snippet: str = "",
) -> str:
    """Internal implementation — see engram_add_evidence for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Register a new source document (webpage, article, data release) in the
    knowledge graph. Creates an Evidence node representing the raw source
    material. Evidence nodes are immutable, URL-keyed references — they record
    that a specific source exists, not what it means or what version was read.
    Per-observation versioning (git_sha + content_hash) lives on observations,
    not evidence (dv_0547).

    Same URL → same evidence node, regardless of content changes. For file-based
    sources, the file must be committed to git before it can be cited (the
    "committed-before-cite" guard remains enforced); the specific revision read
    is recorded on each observation that cites this evidence.

    Args:
        url: Canonical URL of the source document. For local files, use
             'file://path/to/file' format.
        title: Article or page title.
        domain: Source domain (e.g. 'reuters.com'). Auto-extracted from URL if omitted.
        source_date: Publication or byline date of the source (ISO format, e.g. '2026-03-20').
                     Extract from the article's dateline or byline when available.
        content_snippet: Optional truncated excerpt of relevant content for offline re-reading.

    Returns:
        JSON with the evidence node ID and trust pool status.
    """
    if not url or not url.strip():
        return json.dumps({"error": "url is required and cannot be empty."})
    if not title or not title.strip():
        return json.dumps({"error": "title is required and cannot be empty."})
    conn = _get_db()
    try:
        # Compute domain + yellow-card match early so every success return path
        # can surface the warning (short-circuit reuse returns included).
        if not domain:
            domain = _extract_domain(url)
        _yellow_match = _check_yellow_card(domain)

        # Check for existing URL — same URL → same evidence node, always.
        # Per-observation versioning (dv_0547) means content changes don't fork
        # evidence; the new revision is recorded on the observation citing it.
        existing = conn.execute(
            "SELECT id FROM nodes WHERE source_url = ? AND type = 'evidence'"
            " ORDER BY created_at DESC LIMIT 1",
            (url,)
        ).fetchone()
        if existing:
            reuse = {
                "status": "already_exists",
                "evidence_id": existing["id"],
                "message": f"Evidence node {existing['id']} already registered for this URL.",
            }
            if _yellow_match:
                reuse["yellow_card_warning"] = _format_yellow_warning(_yellow_match)
            return json.dumps(reuse)

        # Validate evidence URL resolvability
        RECOGNIZED_SCHEMES = ("http://", "https://", "file://")
        _HONESTY_REMINDER = (
            "\n\nRemember: cheating blinds you. A fabricated URL corrupts your own memory "
            "and every future step built on it. If the real source is hard to find, "
            "raise the problem transparently — never solve it with a shortcut. (ax_0002)"
        )
        if url.startswith("file://"):
            # File:// URLs: enforce the committed-before-cite guard. Do NOT
            # store the version state on the evidence — that lives on each
            # citing observation now.
            ver = _capture_file_version(url)
            if not ver["ok"]:
                return json.dumps({"error": ver["error"]})
        elif url.startswith("http://") or url.startswith("https://"):
            # --- Validation for web URLs ---
            parsed_url = urlparse(url)
            hostname = parsed_url.hostname or ""

            # Check 1: Structural — must have a valid hostname
            if not hostname:
                return json.dumps({
                    "error": f"Malformed URL '{url}' — no hostname found. "
                    "Evidence URLs must point to real, resolvable web addresses."
                    + _HONESTY_REMINDER
                })

            # Check 2: TLD — hostname must contain a dot (rejects fabricated
            # single-label hosts like 'session-transcript', 'conversation')
            if "." not in hostname:
                return json.dumps({
                    "error": f"Invalid hostname '{hostname}' in URL — no TLD found. "
                    "This looks like a fabricated domain. Real web URLs have a domain "
                    "with a TLD (e.g., 'arxiv.org', 'reuters.com')."
                    + _HONESTY_REMINDER
                })

            # Check 3: DNS resolution — verify the domain actually exists.
            # If the agent is citing an online source, network should be available.
            # A DNS failure almost certainly means a fabricated or hallucinated domain.
            try:
                socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            except socket.gaierror:
                return json.dumps({
                    "error": f"DNS resolution failed for '{hostname}' — this domain does not exist. "
                    "The URL may be fabricated or hallucinated. Verify you have the correct URL "
                    "from a real source before citing it as evidence."
                    + _HONESTY_REMINDER
                })
            except (socket.timeout, OSError):
                # Network issues — warn but don't block, since the domain might be real
                pass

        elif not any(url.startswith(s) for s in RECOGNIZED_SCHEMES):
            return json.dumps({
                "error": f"Unrecognized URL scheme in '{url}'. Evidence URLs must be resolvable — "
                "use file:// for local files or https:// for web sources. "
                "Do not invent synthetic schemes (e.g., conversation://, measurement://)."
                + _HONESTY_REMINDER
            })

        # Check trust pool (domain already computed above for yellow-card)
        config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        trust_pool = config.get("trust_pool", [])
        in_trust_pool = any(domain.endswith(tp) for tp in trust_pool)

        node_id = _next_id(conn, "evidence")
        now = _now()

        # Auto-tag source_type from URL pattern (orthogonal to source_class).
        # Used by the dedup heuristic to grade action_hints by artifact kind.
        source_type = _infer_source_type(url)

        conn.execute(
            """INSERT INTO nodes (id, type, created_at, source_url, source_title,
               source_domain, source_date, source_accessed, content_snippet, metadata,
               source_type)
               VALUES (?, 'evidence', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, now, url, title, domain, source_date or None, now,
             content_snippet or None, "{}",
             source_type),
        )

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        conn.commit()

        result = {
            "status": "created",
            "evidence_id": node_id,
            "domain": domain,
            "source_date": source_date or None,
            "in_trust_pool": in_trust_pool,
        }
        if not in_trust_pool:
            result["warning"] = (
                f"Domain '{domain}' is NOT in the trusted source pool. "
                "Consider whether this source meets your quality standards."
            )
        if _yellow_match:
            result["yellow_card_warning"] = _format_yellow_warning(_yellow_match)
        return json.dumps(result)
    finally:
        conn.close()


# Set of legitimate field names for the engram_add_observation payload.
# Used by the MCP wrapper to reject unknown fields up-front (catches typos
# before they reach the impl signature where they'd raise TypeError).
_ADD_OBSERVATION_FIELDS = frozenset({
    "quoted_text", "interpretation", "claim", "quote_type",
    "url", "title", "domain", "source_date", "evidence_id",
    "is_predictive", "predicted_event", "resolution_timeframe",
    "source_class", "content_hash", "git_sha",
})


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
    """Extract and record a claim from a source document with full provenance.

    Creates an Observation node representing the agent's interpretation of a
    specific passage from an evidence source. Every observation has three parts:
    the exact quoted text, the agent's interpretation, and the atomic claim.

    Single-payload signature: pass ALL fields as one JSON object string in
    `payload_json`. This eliminates the antml-prefix multi-parameter swallow
    risk that the previous N-parameter signature was prone to (see ob_1499).

    Source identification: provide EITHER url+title (the evidence node will be
    auto-created or reused if the URL already exists) OR evidence_id (to cite
    an already-registered evidence node). If both are provided, evidence_id
    takes precedence.

    If is_predictive is True, also creates or links to a Prediction node,
    decomposing the forward-looking claim into a factual observation (who
    predicted it) plus a prediction target (what was predicted).

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


def _add_observation_impl(
    quoted_text: str = "",
    interpretation: str = "",
    claim: str = "",
    quote_type: str = "",
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    evidence_id: str = "",
    is_predictive: bool = False,
    predicted_event: str = "",
    resolution_timeframe: str = "",
    source_class: str = "external",
    content_hash: str = "",
    git_sha: str = "",
) -> str:
    """Internal implementation — see engram_add_observation MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers (engram_add_observation_batch, engram_retract replacement path).
    """
    # Per ls_0034: bool-string truthy trap at wrapper boundary. JSON-string
    # "false" is truthy in Python, so an `is_predictive: "false"` payload
    # would silently create an observation_predictive node. Same fix shape
    # as PR #68 (is_self). Check here at the impl entry so both the
    # single-observation wrapper AND the batch processor get protection.
    if not isinstance(is_predictive, bool):
        return json.dumps({
            "error": (
                f"is_predictive must be a JSON boolean (true/false), got "
                f"{type(is_predictive).__name__}: {is_predictive!r}. "
                "Some MCP clients emit booleans as strings — make sure your "
                "JSON encodes `true`/`false` not `\"true\"`/`\"false\"`."
            )
        })

    missing = [
        name for name, val in (
            ("quoted_text", quoted_text),
            ("interpretation", interpretation),
            ("claim", claim),
            ("quote_type", quote_type),
        ) if not val
    ]
    if missing:
        return json.dumps({
            "error": f"Missing required fields: {', '.join(missing)}"
        })
    if quote_type not in VALID_QUOTE_TYPES:
        return json.dumps(
            {
                "error": f"Invalid quote_type '{quote_type}'. Must be one of: {', '.join(sorted(VALID_QUOTE_TYPES))}"
            }
        )

    if source_class not in VALID_SOURCE_CLASSES:
        return json.dumps(
            {
                "error": f"Invalid source_class '{source_class}'. Must be one of: {', '.join(sorted(VALID_SOURCE_CLASSES))}"
            }
        )

    # Resolve evidence node: either from evidence_id or auto-create from url+title
    if not evidence_id and not url:
        return json.dumps(
            {"error": "Provide either 'url' (+ 'title') to auto-create an evidence node, or 'evidence_id' to cite an existing one."}
        )

    if not evidence_id and url and not title:
        return json.dumps(
            {"error": "'title' is required when providing 'url' for auto-creating an evidence node."}
        )

    _yellow_forward = None
    if not evidence_id and url:
        # Auto-create or reuse evidence node
        ev_result = json.loads(_add_evidence_impl(
            url=url, title=title, domain=domain, source_date=source_date,
        ))
        if ev_result.get("error"):
            return json.dumps({"error": f"Failed to create evidence node: {ev_result['error']}"})
        evidence_id = ev_result["evidence_id"]
        _yellow_forward = ev_result.get("yellow_card_warning")

    conn = _get_db()
    try:
        # Validate evidence exists
        ev = conn.execute(
            "SELECT id, source_url FROM nodes WHERE id = ? AND type = 'evidence'",
            (evidence_id,),
        ).fetchone()
        if not ev:
            return json.dumps(
                {"error": f"Evidence node '{evidence_id}' not found."}
            )

        # Verify quoted_text exists in the evidence source (file:// only)
        ev_url = ev["source_url"] or ""
        quote_error = _verify_quote_in_source(ev_url, quoted_text)
        if quote_error:
            return json.dumps({"error": quote_error})

        # Capture per-observation file version (dv_0547). For file:// URLs
        # in a git repo: enforce committed-before-cite + record the specific
        # revision read on this observation. For other URLs: no-op.
        ver = _capture_file_version(ev_url, content_hash=content_hash, git_sha=git_sha)
        if not ver["ok"]:
            return json.dumps({"error": ver["error"]})
        captured_content_hash = ver["content_hash"]
        captured_git_sha = ver["git_sha"]

        node_type = "observation_predictive" if is_predictive else "observation_factual"
        confidence = _compute_confidence(
            conn, node_type, quote_type=quote_type, is_predictive=is_predictive,
            source_class=source_class,
        )

        # Similarity check — run BEFORE insert so the new node doesn't
        # match itself. Results are advisory (non-blocking). Helper
        # extracted 2026-05-14 (#143 §3.1) — same shape used by
        # engram_add_lesson; the importance-floor-in-FTS divergence
        # between callers was accidental drift and is now uniform.
        similar_matches = _similar_existing_matches(
            conn, claim,
            type_filter={"observation_factual", "observation_predictive"},
            extra_columns=("evidence_id",),
        )

        node_id = _next_id(conn, node_type)
        now = _now()
        obs_metadata = {"source_class": source_class}
        if captured_git_sha:
            obs_metadata["git_sha"] = captured_git_sha
        if captured_content_hash:
            obs_metadata["content_hash"] = captured_content_hash
        metadata = json.dumps(obs_metadata)
        conf_reason = f"Initial: {quote_type}"
        if source_class != "external":
            conf_reason += f" (source_class: {source_class})"

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, evidence_id, quoted_text,
               interpretation, quote_type, confidence, confidence_history, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                node_type,
                claim,
                now,
                evidence_id,
                quoted_text,
                interpretation,
                quote_type,
                confidence,
                json.dumps([{"timestamp": now, "value": confidence, "reason": conf_reason}]),
                metadata,
            ),
        )

        # Edge: observation cites evidence
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
            (node_id, evidence_id, now),
        )

        # Stamp importance
        _stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)

        result = {
            "status": "created",
            "observation_id": node_id,
            "evidence_id": evidence_id,
            "type": node_type,
            "confidence": confidence,
            "source_class": source_class,
        }

        # Handle prediction decomposition
        prediction_id = None
        if is_predictive and predicted_event:
            # Check if a prediction node for this event already exists
            existing_pred = conn.execute(
                """SELECT id FROM nodes WHERE type = 'prediction'
                   AND predicted_event = ? AND is_current = 1""",
                (predicted_event,),
            ).fetchone()

            if existing_pred:
                prediction_id = existing_pred["id"]
                result["prediction_status"] = "linked_to_existing"
            else:
                prediction_id = _next_id(conn, "prediction")
                conn.execute(
                    """INSERT INTO nodes (id, type, predicted_event, resolution_timeframe,
                       status, created_at, confidence)
                       VALUES (?, 'prediction', ?, ?, 'open', ?, NULL)""",
                    (prediction_id, predicted_event, resolution_timeframe or None, now),
                )
                result["prediction_status"] = "created"
                _log_edit(conn, "created", prediction_id, "prediction",
                          {"predicted_event": predicted_event[:200]})

            # Edge: observation supports prediction
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                    (node_id, prediction_id, now),
                )
            except sqlite3.IntegrityError:
                pass

            result["prediction_id"] = prediction_id

        _stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)

        # Attach similarity results with decision guidance.
        #
        # Action hints are graded by THREE dimensions, not just same_evidence:
        #   1. match_type — keyword (FTS, no similarity score) vs semantic (cosine sim)
        #   2. similarity score — only meaningful for semantic matches
        #   3. source_type of the shared evidence — document vs conversation/file
        #
        # Why: the old binary "same_evidence → DUPLICATE" rule caused alarm
        # fatigue because long conversation/file evidence (chat logs, edited
        # source files) legitimately produces many distinct claims from one
        # source. The new logic only cries DUPLICATE when the source is a
        # discrete document AND similarity is high — the pattern the original
        # rule was actually designed for.
        if similar_matches:
            # Look up the source_type of the current observation's evidence
            own_st_row = conn.execute(
                "SELECT source_type FROM nodes WHERE id = ?", (evidence_id,)
            ).fetchone()
            own_source_type = (
                (own_st_row["source_type"] if own_st_row and own_st_row["source_type"] else None)
                or "document"
            )
            # Hoist config out of the per-candidate loop — for K=15 candidates
            # this avoids 15x CONFIG_PATH.read_text() + json.loads() per write.
            _th = _get_thresholds_config()
            # Pre-compute polarity alerts in a single batched NLI forward pass
            # for ALL eligible candidates (those above min_similarity_for_check).
            # ~33% latency win vs per-candidate single-pair calls per the
            # bake-off v2 measurement (ModernCE-large 16.6ms single → 11.1ms
            # batched on RTX 5090).
            _polarity_alerts = _compute_polarity_alerts(claim, similar_matches)

            for m in similar_matches:
                same_ev = (m.get("evidence_id") == evidence_id)
                m["same_evidence"] = same_ev
                sim = m.get("similarity")  # None for FTS keyword-only matches

                if sim is None:
                    # FTS keyword match: coarse signal, no cosine score
                    if same_ev:
                        m["action_hint"] = (
                            f"KEYWORD_OVERLAP_SAME_SOURCE — keyword match with {m['id']} "
                            f"in the same {own_source_type}. Inspect to decide if duplicate or distinct."
                        )
                    else:
                        m["action_hint"] = (
                            f"KEYWORD_OVERLAP_OTHER_SOURCE — keyword match with {m['id']} "
                            f"from a different source. Inspect to decide if corroboration."
                        )
                    continue

                # Semantic match — graded by similarity AND source_type
                #
                # Discrete artifacts (document, web_page): an author writes
                # once, so high-similarity within the same source is usually a
                # duplicate worth flagging.
                #
                # Long-running sources (conversation, file): many distinct
                # claims legitimately share one source (chat logs, edited code
                # files), so DUPLICATE only fires at very high similarity.
                if same_ev:
                    if own_source_type in ("document", "web_page") and sim >= 0.85:
                        m["action_hint"] = (
                            f"DUPLICATE — high-similarity ({sim:.2f}) claim from the same {own_source_type}. "
                            f"Likely the author repeating themselves. Consider retracting this new observation."
                        )
                    elif own_source_type in ("conversation", "file") and sim >= 0.92:
                        m["action_hint"] = (
                            f"POSSIBLE_DUPLICATE — very-high similarity ({sim:.2f}) "
                            f"with {m['id']} from the same {own_source_type}. "
                            f"Inspect both before deciding — long-running sources can repeat."
                        )
                    else:
                        m["action_hint"] = (
                            f"DISTINCT_FROM_SAME_SOURCE — same {own_source_type} source, similarity {sim:.2f}. "
                            f"Likely a different fact from a long-running source. "
                            f"Create normally if the claim adds new information."
                        )
                else:
                    if sim >= float(_th["action_hint_corroborate"]):
                        m["action_hint"] = (
                            f"CORROBORATE — high-similarity ({sim:.2f}) claim from an independent source. "
                            f"Create a corroboration derivation (inductive_generalization) "
                            f"citing both {m['id']} and {node_id}."
                        )
                    elif sim >= float(_th["action_hint_related"]):
                        m["action_hint"] = (
                            f"RELATED — moderate similarity ({sim:.2f}) from a different source. "
                            f"Inspect to decide whether corroboration is warranted."
                        )
                    else:
                        m["action_hint"] = (
                            f"WEAK_MATCH — low similarity ({sim:.2f}). "
                            f"Probably unrelated. Ignore unless inspection suggests otherwise."
                        )

                # Polarity NLI overlay — applies to BOTH same-source and
                # cross-source semantic matches (per Lei 2026-05-10:
                # "same source still worth marking contradiction"). Prepended
                # to whichever tier hint was set above. Pre-computed in a single
                # batched NLI call above (_compute_polarity_alerts); look up by
                # candidate id here. Candidates filtered out (sim below floor,
                # polarity disabled, model load failure, score below threshold)
                # are simply not in the dict.
                polarity_prefix = _polarity_alerts.get(m["id"])
                if polarity_prefix:
                    m["action_hint"] = polarity_prefix + m["action_hint"]

            result["similar_existing"] = similar_matches
            result["similar_count"] = len(similar_matches)
            result["dedup_guidance"] = (
                "Similarity-graded matches. Action hints account for source type "
                "(document vs conversation/file) and similarity score. "
                "DUPLICATE only fires for high-similarity matches in discrete documents."
            )

        if _yellow_forward:
            result["yellow_card_warning"] = _yellow_forward

        conn.commit()

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # Emit AFTER commit per ax_0001 (structural honesty): logging an event
        # that says "this happened" before commit succeeds would risk
        # asserting a falsehood if the commit raises.
        # Decision context: similar-existing hits + dedup hint + result.
        # emit_if_initialized is a silent no-op when the server-side emitter
        # is not yet initialized (Phase 4 will add the init path).
        _similar_count = len(similar_matches)
        _highest_sim = None
        _first_hint = None
        if similar_matches:
            sims = [m.get("similarity") for m in similar_matches if m.get("similarity") is not None]
            if sims:
                _highest_sim = max(sims)
            _first_hint = similar_matches[0].get("action_hint")
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_add_observation",
                "similar_existing_returned_count": _similar_count,
                "highest_similarity": _highest_sim,
                "dedup_action_hint": _first_hint,
                "result_status": result.get("status"),
                "result_node_id": result.get("observation_id"),
                "validation_warnings": (
                    [result["yellow_card_warning"]] if result.get("yellow_card_warning") else []
                ),
            },
        )
        return json.dumps(result)
    finally:
        conn.close()


# ---- Batch ingestion tools ----


# Set of legitimate field names for the engram_add_observation_batch payload.
_ADD_OBSERVATION_BATCH_FIELDS = frozenset({
    "observations_json", "url", "title", "domain", "source_date",
    "evidence_id", "content_hash", "git_sha",
})


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

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. The inner
    `observations_json` field is itself a stringified JSON array of
    observation objects (nested-encoding pattern matching engram_retract's
    `replacement_json`).

    Args:
        payload_json: JSON object (as a string) with these fields:
            observations_json (str, required): Stringified JSON array of
                observation objects. Each observation must have quoted_text,
                interpretation, claim, quote_type; optional is_predictive,
                predicted_event, resolution_timeframe, source_class.
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


def _add_observation_batch_impl(
    observations_json: str = "",
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    evidence_id: str = "",
    content_hash: str = "",
    git_sha: str = "",
) -> str:
    """Internal implementation — see engram_add_observation_batch MCP tool for
    the public payload schema. Kept callable with named kwargs for in-server
    callers.

    Extract multiple observations from a single source in one call. Use this
    after reading a source holistically — extract ALL substantive claims at
    once rather than calling engram_add_observation repeatedly. All
    observations in the batch cite the same source.

    Source identification: provide EITHER url+title (the evidence node will be
    auto-created or reused) OR evidence_id. If both are provided, evidence_id
    takes precedence.

    Args:
        observations_json: JSON array of observation objects, each with:
            - quoted_text (required): Verbatim quote from the source
            - interpretation (required): Your reasoning about what this means
            - claim (required): Atomic, falsifiable claim
            - quote_type (required): hard_data, official_statement, attributed_analysis, unnamed_source, personal_communication, or editorial
            - is_predictive (optional): boolean, default false
            - predicted_event (optional): if predictive, the event predicted
            - resolution_timeframe (optional): if predictive, ISO date for resolution
            - source_class (optional): 'external' (default), 'introspective', or 'user_stated'
            - content_hash (optional): SHA-256 of file content for file-based evidence
            - git_sha (optional): Git commit SHA for file-based evidence
        url: URL of the source document. The evidence node is auto-created or reused.
        title: Title of the source document (required if url is provided).
        domain: Source domain (auto-extracted from URL if omitted).
        source_date: Publication date in ISO format (e.g. '2026-03-20').
        evidence_id: ID of an existing evidence node. Use this OR url+title, not both.

    Example:
        observations_json = '[{"quoted_text": "Gold fell 9.6%", "interpretation": "Worst weekly decline", "claim": "Gold declined 9.6% this week", "quote_type": "hard_data"}]'

    Returns:
        JSON with list of created observation nodes, their confidence scores, and any predictions.
    """
    try:
        observations = json.loads(observations_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(observations, list):
        return json.dumps({"error": "observations_json must be a JSON array."})

    # Resolve evidence node once for the whole batch
    resolved_evidence_id = evidence_id
    evidence_created = False
    _batch_yellow = None
    if not resolved_evidence_id and url:
        if not title:
            return json.dumps({"error": "'title' is required when providing 'url'."})
        ev_result = json.loads(_add_evidence_impl(
            url=url, title=title, domain=domain, source_date=source_date,
        ))
        if ev_result.get("error"):
            return json.dumps({"error": f"Failed to create evidence node: {ev_result['error']}"})
        resolved_evidence_id = ev_result["evidence_id"]
        evidence_created = ev_result["status"] == "created"
        _batch_yellow = ev_result.get("yellow_card_warning")
    elif not resolved_evidence_id:
        return json.dumps({"error": "Provide either 'url' (+ 'title') or 'evidence_id'."})

    results = []
    for i, obs in enumerate(observations):
        if not isinstance(obs, dict):
            results.append({"index": i, "error": "Each observation must be a JSON object."})
            continue

        required = ["quoted_text", "interpretation", "claim", "quote_type"]
        missing = [f for f in required if not obs.get(f)]
        if missing:
            results.append({"index": i, "error": f"Missing required fields: {', '.join(missing)}"})
            continue

        r = json.loads(_add_observation_impl(
            evidence_id=resolved_evidence_id,
            quoted_text=obs["quoted_text"],
            interpretation=obs["interpretation"],
            claim=obs["claim"],
            quote_type=obs["quote_type"],
            is_predictive=obs.get("is_predictive", False),
            predicted_event=obs.get("predicted_event", ""),
            resolution_timeframe=obs.get("resolution_timeframe", ""),
            source_class=obs.get("source_class", "external"),
            content_hash=content_hash,
            git_sha=git_sha,
        ))
        r["index"] = i
        results.append(r)

    created = [r for r in results if r.get("status") == "created"]
    batch_result = {
        "status": "batch_complete",
        "evidence_id": resolved_evidence_id,
        "evidence_created": evidence_created,
        "total": len(observations),
        "created": len(created),
        "results": results,
    }
    if _batch_yellow:
        batch_result["yellow_card_warning"] = _batch_yellow

    # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
    # Batch-level summary event — one event for the entire batch MCP call.
    # Per-observation events are already emitted by _add_observation_impl above.
    emit_if_initialized(
        event_type="engram.tool.engram_call",
        level=1,
        data={
            "tool_name": "engram_add_observation_batch",
            "result_status": "batch_complete",
            "result_node_id": None,
            "batch_total": len(observations),
            "batch_created": len(created),
            "batch_error_count": len(observations) - len(created),
            "validation_warnings": (
                [batch_result["yellow_card_warning"]]
                if batch_result.get("yellow_card_warning")
                else []
            ),
        },
    )

    return json.dumps(batch_result)


# ---- MMR diversity reranker helpers (alpha #178 area 2) ----


def _decode_embedding(raw) -> list[float] | None:
    """Decode a node's embedding field to a list[float], or None if unavailable.

    The embedding column stores JSON-serialised float arrays (set by
    _compute_and_store_embedding).  Handles three cases:
      - str  → JSON-parse to list[float]
      - list → already decoded (e.g. when a caller stored the raw list)
      - None / anything else → return None (cold-start; no diversity penalty)
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return decoded
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _max_cosine_to_selected(candidate: dict, selected: list[dict]) -> float:
    """Return max cosine similarity between candidate and any already-selected node.

    Uses EmbeddingManager.cosine_similarity (pure Python, no numpy dep).
    Returns 0.0 when either embedding is absent (cold-start: no penalty).
    """
    e_c = _decode_embedding(candidate.get("_embedding"))
    if e_c is None:
        return 0.0
    best = 0.0
    for s in selected:
        e_s = _decode_embedding(s.get("_embedding"))
        if e_s is None:
            continue
        sim = _embedder.cosine_similarity(e_c, e_s)
        if sim > best:
            best = sim
    return best


def _mmr_rerank(
    candidates: list[dict],
    top_k: int,
    mmr_lambda: float,
) -> list[dict]:
    """Maximal Marginal Relevance reranker (Tier 3 of the ranking pipeline).

    Selects up to top_k nodes from candidates, greedily maximising:

        mmr(c) = composite(c) × (1 − (1 − mmr_lambda) × max_sim(c, selected))

    where composite(c) is the pre-computed _composite score and max_sim is
    the highest cosine similarity between c and any already-selected node.

    Input contract:
        Each candidate has "_composite" (float) annotated by the upstream
        composite phase. The caller (_search_nodes) is expected to have
        sorted the input by _composite desc — MMR's first-slot pick relies
        on candidate[0] being the highest-composite. As a safety net, MMR
        re-sorts internally; the upstream sort + internal sort are
        intentionally redundant so neither side has to trust the other's
        invariant silently.
        Each candidate may optionally have "_embedding" (decoded list[float]
        or raw JSON str); when absent, max_sim=0 → no diversity penalty.

    Args:
        candidates: list of node dicts, sorted by _composite desc.
        top_k: desired output size (≤ len(candidates)).
        mmr_lambda: ∈ [0, 1].  1 = pure composite (no diversity); 0 = full
            diversity penalty.

    Returns:
        MMR-ordered list, length ≤ top_k.  The "_embedding" key is preserved
        on each dict so the caller can strip it after the full pipeline.
    """
    if not candidates:
        return []
    remaining = list(candidates)
    # Defensive re-sort: pre-MMR sort by the caller is the documented input
    # contract (see docstring), but the cost of re-sorting is negligible and
    # the redundancy protects against either side's invariant breaking
    # silently.  First slot is highest composite (before diversity matters).
    remaining.sort(key=lambda c: c.get("_composite", 0.0), reverse=True)
    selected: list[dict] = [remaining.pop(0)]
    while remaining and len(selected) < top_k:
        best: dict | None = None
        best_score = float("-inf")
        for c in remaining:
            max_sim = _max_cosine_to_selected(c, selected)
            mmr_score = c.get("_composite", 0.0) * (1.0 - (1.0 - mmr_lambda) * max_sim)
            if mmr_score > best_score:
                best_score = mmr_score
                best = c
        if best is None:
            break
        remaining.remove(best)
        selected.append(best)
    return selected


# ---- Internal search layer ----


def _search_nodes(
    conn: sqlite3.Connection,
    query: str,
    types: str = "",
    min_confidence: float = 0.0,
    include_superseded: bool = False,
    top_k: int = 10,
    use_semantic: bool = True,
    embed_query: str | None = None,
    *,
    return_debug: bool = False,
) -> dict:
    """Pure FTS5 + semantic search.  No side effects (no refresh, no neighbor expansion).

    Args:
        query: the FTS5 keyword query (also used as semantic query when
            embed_query is None — backward-compat default).
        embed_query: optional separate semantic-search query string. When
            provided, semantic search uses this instead of `query`. Used by
            the auto-surface hook for short prompts that benefit from
            prev-response-tail prepending on the embedding side WITHOUT
            polluting FTS (which stays keyword-matched against `query`).
            See alpha #177 + ~/borges-desk/projects/auto-surface-prepending/.
        return_debug: if True, include a ``debug`` key in the returned dict
            with intermediate ranking signals (keyword extraction, FTS/semantic
            candidate counts, per-result scores). Default False — never bloats
            normal responses. Used by SxS comparison UI and retrieval diagnostics.

    Returns dict with:
        results: list of node dicts (from _node_to_dict) with match_type/similarity added
        keyword_matches: count of FTS hits
        semantic_matches: count of semantic-only hits
        semantic_available: bool
        debug: (only present when return_debug=True) intermediate ranking signals

    Three-tier ranking pipeline (Lei 2026-05-19 architecture) with special-type
    bypass (Lei 2026-05-19 PM, the child case):

        TIER 1 — Raw retrieval (FTS + semantic, lossy floors only)
            ── FTS5 keyword search: BM25-ranked, capped at (top_k × TIER1_MULTIPLIER)//2,
               post-filtered for type/superseded/confidence/tier2.
            ── Semantic search: cosine-similarity-ranked, min_similarity =
               FTS_SIM_FLOOR (0.30), overfetch to max(top_k × TIER1_MULTIPLIER, 100).
            ── Merge: FTS hits first, then semantic-only; FTS hits get
               relevance via floor+bump (alpha #207/#208), semantic-only
               keep their cosine similarity.
            Output contract: ≤ top_k × TIER1_MULTIPLIER candidates after merge cap;
            unsorted list with _relevance + optional _embedding annotated.

        SPECIAL-TYPE PARTITION — After Tier 1, split the pool by type:
            ── special_pool: types in SPECIAL_TYPES_BYPASS (person, definition, goal,
               axiom, contradiction, question, conjecture, lesson).
               These skip Tiers 2 + 3 entirely. Filter: similarity >= FTS_SIM_FLOOR.
               Sort by similarity desc. All qualifying nodes surface regardless of
               how many generic results exist.
            ── generic_output: everything else (dominantly observation_factual,
               derivation). Proceeds through Tiers 2 + 3 as usual.

        TIER 2 — Composite ranking applied to generic_output only
            composite = relevance × util_amp × imp_amp
                      = relevance × (1 + UTIL_BETA × utility_score)
                                  × (1 + IMP_BETA × normalized_importance)
            Then SORT + CUT to top_k × TIER2_MULTIPLIER.
            Output contract: top_k × TIER2_MULTIPLIER candidates sorted by _composite desc.

        TIER 3 — MMR diversity rerank applied to generic_output only
            mmr(c) = composite(c) × (1 − (1 − MMR_LAMBDA) × max_sim(c, selected))
            Greedy selection of top_k from the TIER2-sized pool.
            Output contract: top_k candidates, MMR-ordered.

        FINAL MERGE — generic_output (top_k) + special_pool (all qualifying).
            top_k is the GENERIC-result budget; special-type results are
            always-include, so len(results) may exceed top_k.
            Callers needing strict top_k can slice further.
            Dedup by node ID (should not occur in practice since types partition cleanly).

        Post-pipeline strip: remove _composite + _embedding internal keys.

    Boundary contracts are explicit (sort before MMR, MMR documents
    the expectation) — see Lei's "quick_sort on 100 items is cheap,
    failed-assumption is devastating" framing 2026-05-19.
    """
    # Default embed_query to query if not explicitly provided (backward compat).
    effective_embed_query = embed_query if embed_query is not None else query
    type_filter = set(t.strip() for t in types.split(",") if t.strip()) if types else None
    tier2_threshold = _get_tier_threshold(conn, 2)

    # --- Debug signal capture (pre-search) ---
    # Collected here, populated incrementally, assembled at return.
    _dbg_extracted_keywords: list = []
    _dbg_fts_candidate_count: int = 0
    _dbg_semantic_candidate_count: int = 0
    _dbg_fts_cap_value: Optional[int] = None
    # Per-result debug signals keyed by node ID (populated during merge loops).
    _dbg_per_result: dict[str, dict] = {}

    if return_debug:
        # Extract keywords using the same function _sanitize_fts_query calls
        # internally — captures its output without modifying it.
        try:
            from engram_idf import extract_keywords
            _dbg_extracted_keywords = list(extract_keywords(conn, query, min_idf=4.0, top_k=5))
        except Exception:
            _dbg_extracted_keywords = []

    # --- Tier-size derivation (Lei 2026-05-19 architecture discussion) ---
    # Tier 1: large raw pool per source.  Tier 2: composite-shrink target.
    # Tier 3 returns top_k (the caller's requested count).
    tier1_size = top_k * TIER1_MULTIPLIER   # e.g. 300 for top_k=10
    tier2_size = top_k * TIER2_MULTIPLIER   # e.g.  40 for top_k=10

    # ══ TIER 1: Raw retrieval (FTS + semantic) ══════════════════════════
    # Retrieve a large raw pool per source (TIER1_MULTIPLIER × top_k per path).
    # Output: ≤ tier1_size candidates with _relevance + optional _embedding;
    # order is interleaved (FTS-first then semantic-only), NOT sorted.

    # --- FTS keyword search ---
    fts_ids: set[str] = set()
    fts_results: list[sqlite3.Row] = []
    fts_query = _sanitize_fts_query(query, conn)
    fts_cap = max(1, tier1_size // 2)  # reservation cap for FTS (half of tier-1 pool)
    try:
        if not fts_query:
            raise sqlite3.OperationalError("empty FTS query")
        # LIMIT is tier1_size * 3 (= top_k * TIER1_MULTIPLIER * 3) so the
        # SQL fetch always supplies enough rows for fts_cap (= tier1_size // 2)
        # to be the binding constraint, regardless of TIER1_MULTIPLIER's
        # value. Previously hardcoded as top_k * 3 — became the binding
        # constraint at the old MMR_POOL_MULTIPLIER ≥ 3.
        fts_rows = conn.execute(
            """SELECT n.*, fts.rank
               FROM nodes_fts fts
               JOIN nodes n ON n.rowid = fts.rowid
               WHERE nodes_fts MATCH ?
               ORDER BY fts.rank
               LIMIT ?""",
            (fts_query, tier1_size * 3),
        ).fetchall()

        if return_debug:
            _dbg_fts_candidate_count = len(fts_rows)

        for r in fts_rows:
            if type_filter and r["type"] not in type_filter:
                continue
            if not include_superseded and not r["is_current"]:
                continue
            if r["confidence"] is not None and r["confidence"] < min_confidence:
                continue
            if tier2_threshold > 0 and (r["importance_score"] or 0) < tier2_threshold:
                continue
            fts_results.append(r)
            fts_ids.add(r["id"])
            # Cap FTS at half of tier1_size so semantic results retain real estate
            # in the merged output. With the new IDF-driven OR-match,
            # _sanitize_fts_query can return 40-100+ hits for a single query;
            # without this cap, FTS would fill every slot and crowd out the
            # high-quality semantic matches that carried the baseline.
            # The composite re-rank at the merge tail still sorts the
            # combined pool, so strong FTS hits can still outrank weak
            # semantic ones — the cap just ensures both paths get a seat.
            if len(fts_results) >= fts_cap:
                break

        if return_debug and len(fts_results) >= fts_cap:
            _dbg_fts_cap_value = fts_cap

    except sqlite3.OperationalError:
        pass  # FTS query syntax error — fall through to semantic

    # --- Semantic search (complement, not replace) ---
    # Uses effective_embed_query (= explicit embed_query if provided, else
    # the same query as FTS). This split lets the auto-surface hook prepend
    # prev-response-tail to the SEMANTIC query for short prompts without
    # polluting FTS keyword matching with arbitrary prior-context tokens.
    semantic_only: list[dict] = []
    # Similarity lookup for FTS-hit nodes — populated below from the semantic
    # search results. Used to apply the floor + multiplicative bump (alpha
    # #207/#208) when merging FTS hits with semantic hits.
    sim_lookup: dict[str, float] = {}
    semantic_available = use_semantic and _embedder.is_available() and _get_embedding_config().get("enabled", True)
    if semantic_available:
        # Overfetch semantic so FTS-hit similarities (if above floor) are
        # available for the bump computation. min_similarity=FTS_SIM_FLOOR
        # naturally drops "truly unrelated" candidates so we don't waste rows.
        sem_results = _semantic_search(
            conn, effective_embed_query,
            top_k=max(top_k * TIER1_MULTIPLIER, 100),
            min_similarity=FTS_SIM_FLOOR,
            type_filter=type_filter,
            importance_threshold=tier2_threshold,
        )
        if return_debug:
            _dbg_semantic_candidate_count = len(sem_results)
        for s in sem_results:
            sim_lookup[s["id"]] = s["similarity"]
            if s["id"] not in fts_ids:
                if s["confidence"] is not None and s["confidence"] < min_confidence:
                    continue
                semantic_only.append(s)

    # --- Merge: FTS first, then semantic-only ---
    # FTS-merge formula (alpha #207/#208 mitigation):
    #   if no semantic available: fall back to old 1 - 1/(1+|bm25|) normalization
    #   else: floor at FTS_SIM_FLOOR, multiplicative bump on sim
    #     final = min(1.0, sim * (1.0 + sqrt(|bm25|) / sqrt(FTS_BUMP_NORMALIZER)))
    #   Floor drops nodes where semantic says "truly unrelated" regardless of
    #   how strong FTS appears (closes the q_0077 / ob_1152 leak).
    output: list[dict] = []
    seen: set[str] = set()

    for r in fts_results:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        bm25_rank = r["rank"]  # negative float, more-negative = better
        node_sim = sim_lookup.get(r["id"]) if semantic_available else None

        if semantic_available:
            if node_sim is None or node_sim < FTS_SIM_FLOOR:
                # Semantic says off-topic — drop regardless of FTS strength
                continue
            bump = min(1.0, math.sqrt(abs(bm25_rank)) / math.sqrt(FTS_BUMP_NORMALIZER))
            relevance_norm = min(1.0, node_sim * (1.0 + bump))
        else:
            # Fallback: old BM25-only normalization (semantic unavailable)
            relevance_norm = 1.0 - 1.0 / (1.0 + abs(bm25_rank))

        node = _node_to_dict(r)
        # Preserve embedding for MMR cosine computation (stripped after MMR).
        # r is an sqlite3.Row from SELECT n.* which includes the embedding col.
        raw_emb = r["embedding"] if "embedding" in r.keys() else None
        if raw_emb is not None:
            node["_embedding"] = raw_emb
        node["match_type"] = "keyword"
        node["_relevance"] = relevance_norm
        if node_sim is not None:
            node["similarity"] = node_sim
        if return_debug:
            _dbg_per_result[r["id"]] = {
                "id": r["id"],
                # Use the same "keyword" label the result-node API uses
                # (line 2063) so debug consumers can cross-reference cleanly.
                "match_type": "keyword",
                "bm25_raw": bm25_rank,
                "relevance_normalized": relevance_norm,
                "similarity": node_sim,
                "composite_score": None,   # computed below after re-rank
                "utility_score": node.get("utility_score"),
                "importance_score": node.get("importance_score"),
                "importance_base": node.get("importance_base"),
                "confidence": node.get("confidence"),
                "surprise_score": None,    # not stored as a column
                "recall_turn": node.get("recall_turn"),
            }
        output.append(node)

    # Fill remaining tier-1 slots with semantic-only hits (up to tier1_size total).
    # Large tier-1 pool ensures tier-2 composite shrink has enough candidates.
    remaining = tier1_size - len(output)
    for s in semantic_only[:remaining]:
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        full = conn.execute("SELECT * FROM nodes WHERE id = ?", (s["id"],)).fetchone()
        if full:
            node = _node_to_dict(full)
            # Preserve embedding for MMR cosine computation (stripped after MMR).
            raw_emb = full["embedding"] if "embedding" in full.keys() else None
            if raw_emb is not None:
                node["_embedding"] = raw_emb
            node["match_type"] = "semantic"
            node["similarity"] = s["similarity"]
            node["_relevance"] = s["similarity"]
            if return_debug:
                _dbg_per_result[s["id"]] = {
                    "id": s["id"],
                    "match_type": "semantic",
                    "bm25_raw": None,
                    "relevance_normalized": None,
                    "similarity": s["similarity"],
                    "composite_score": None,   # computed below after re-rank
                    "utility_score": node.get("utility_score"),
                    "importance_score": node.get("importance_score"),
                    "importance_base": node.get("importance_base"),
                    "confidence": node.get("confidence"),
                    "surprise_score": None,    # not stored as a column
                    "recall_turn": node.get("recall_turn"),
                }
            output.append(node)

    # ══ SPECIAL-TYPE PARTITION ══════════════════════════════════════════
    # After Tier 1, split the merged pool into special-type and generic paths.
    # Special types (SPECIAL_TYPES_BYPASS) skip Tiers 2 + 3 entirely: they
    # only need similarity >= FTS_SIM_FLOOR to surface. This ensures sparse
    # anchor-types (person, definition, goal) and status-bearing types
    # (axiom, contradiction, question, conjecture, lesson) are never buried
    # by composite amplifiers on common observation_factual / derivation nodes.
    # the child case (2026-05-19 PM): pn_0005 sim=0.3786 above floor but
    # composite=0.57 loses to goodnight obs composite=0.91.
    #
    # Similarity for a node is in node["similarity"] (set during Tier-1 merge
    # for both FTS-hit and semantic paths). When semantic is unavailable,
    # FTS-only nodes won't have "similarity" set — fall back to treating them
    # as passing the floor (FTS already applied its own relevance gate).
    special_pool: list[dict] = []
    generic_output: list[dict] = []
    for node in output:
        if node.get("type") in SPECIAL_TYPES_BYPASS:
            sim = node.get("similarity")
            # When semantic unavailable, sim is None; treat as passing floor.
            if sim is None or sim >= FTS_SIM_FLOOR:
                special_pool.append(node)
            # If sim is explicitly below floor, drop (inconsistent but defensive).
        else:
            generic_output.append(node)
    # Sort special_pool by similarity desc (None → 0.0 for sort stability).
    special_pool.sort(key=lambda n: n.get("similarity") or 0.0, reverse=True)
    # Strip _relevance from special nodes (not needed — they bypass composite).
    for node in special_pool:
        node.pop("_relevance", None)

    # Diversity-preserving cap (SPECIAL_POOL_CAP). Reserve top-1 per type that
    # has members in the pool, then fill remaining slots from the rest by
    # similarity desc. Preserves the child-case rescue (the single best person
    # node won't be starved by 9 high-sim definitions) while bounding the
    # pool to a sensible size for surface-channel rendering.
    if len(special_pool) > SPECIAL_POOL_CAP:
        seen_types: set[str] = set()
        top_per_type: list[dict] = []
        remainder: list[dict] = []
        for n in special_pool:  # already sim-desc sorted
            ntype = n.get("type", "")
            if ntype not in seen_types:
                seen_types.add(ntype)
                top_per_type.append(n)
            else:
                remainder.append(n)
        # remainder is already sim-desc (preserves the input order from special_pool)
        slots_left = max(0, SPECIAL_POOL_CAP - len(top_per_type))
        capped = top_per_type + remainder[:slots_left]
        # Resort the final cohort by similarity desc for consistent output order.
        capped.sort(key=lambda n: n.get("similarity") or 0.0, reverse=True)
        special_pool = capped

    # Tier 2 + 3 operate on generic_output only.
    output = generic_output

    # ══ TIER 2: Composite ranking (multiplicative amplifiers) ═══════════
    # Lei 2026-05-19. Replaces the additive λ-blend with pure multiplicative
    # composition: relevance is the base, utility + importance are amplifiers.
    #   composite = relevance × (1 + UTIL_BETA × utility_score)
    #                         × (1 + IMP_BETA × normalized_importance)
    # Then SORT + CUT to top_k × TIER2_MULTIPLIER (= 40 default).
    # _composite is NOT popped here — Tier 3 MMR pass needs it.
    # _composite + _embedding stripped after Tier 3 completes.
    min_queryable_imp = _get_min_queryable_importance(conn, tier2_threshold=tier2_threshold)
    imp_norm_factor = 1.0 / min_queryable_imp
    for node in output:
        rel = node.pop("_relevance", 0.5) or 0.0
        util = node.get("utility_score", 0) or 0.0
        imp = node.get("importance_score", 0) or 0.0
        util_amp = 1.0 + UTIL_BETA * util
        imp_amp = 1.0 + IMP_BETA * (imp * imp_norm_factor)
        composite = rel * util_amp * imp_amp
        node["_composite"] = composite
        if return_debug and node.get("id") in _dbg_per_result:
            _dbg_per_result[node["id"]]["composite_score"] = composite
            # NEW debug fields surfacing the amp decomposition for harness diagnostics
            _dbg_per_result[node["id"]]["util_amp"] = util_amp
            _dbg_per_result[node["id"]]["imp_amp"] = imp_amp
            _dbg_per_result[node["id"]]["imp_norm_factor"] = imp_norm_factor
    # Sort by composite desc.  Contract with _mmr_rerank: input is sorted
    # (MMR's first-slot pick assumes candidate[0] is highest-composite).
    # MMR also re-sorts internally as a safety net, but enforcing the sort
    # here makes the boundary explicit and inspectable in debugging.
    output.sort(key=lambda x: x.get("_composite", 0), reverse=True)
    # Tier-2 shrink: cut to top_k × TIER2_MULTIPLIER best-composite candidates.
    # This is the tier-boundary cut — tier-1 retrieved broadly, tier-2 shrinks
    # before handing off to MMR.  The sort above ensures we keep the top.
    output = output[:tier2_size]

    # ── TIER 3: MMR diversity reranker (alpha #178 area 2) ───────────────
    # Input: tier2_size candidates (top_k × TIER2_MULTIPLIER, composite-sorted).
    # Output: top_k nodes, MMR-ordered for diversity.
    # When MMR_LAMBDA=1 the output is identical to the composite-sorted order.
    # When embedder is disabled (ENGRAM_NO_EMBEDDINGS=1) all nodes have
    # _embedding=None → max_sim=0 everywhere → MMR reduces to composite order.
    output = _mmr_rerank(output, top_k=top_k, mmr_lambda=MMR_LAMBDA)

    # Strip internal-only fields before returning to callers.
    for node in output:
        node.pop("_composite", None)
        node.pop("_embedding", None)
    # Strip _embedding from special_pool too (set during Tier 1).
    for node in special_pool:
        node.pop("_embedding", None)

    # ══ FINAL MERGE: generic (top_k) + special-type bypass results ══════
    # top_k is the generic-result budget. Special-type results append after,
    # so len(results) may exceed top_k. Dedup by node ID (types partition
    # cleanly in practice, but the guard is cheap).
    generic_ids = {n["id"] for n in output}
    for node in special_pool:
        if node["id"] not in generic_ids:
            output.append(node)

    # keyword_matches counts post-floor (after the sim<FTS_SIM_FLOOR drop)
    # so callers see the actual contributing FTS pool, not the pre-floor
    # candidate count. Pre-floor count is in `debug.fts_candidate_count`.
    kw_post_floor = sum(1 for n in output if n.get("match_type") == "keyword")
    base_result = {
        "results": output,
        "keyword_matches": kw_post_floor,
        "semantic_matches": len(semantic_only),
        "semantic_available": _embedder.is_available(),
    }

    if return_debug:
        # Build per_result list in the same order as output (post-sort).
        per_result_ordered = [
            _dbg_per_result[node["id"]]
            for node in output
            if node.get("id") in _dbg_per_result
        ]
        base_result["debug"] = {
            "extracted_keywords": _dbg_extracted_keywords,
            "fts_query_string": fts_query,
            "fts_candidate_count": _dbg_fts_candidate_count,
            "semantic_candidate_count": _dbg_semantic_candidate_count,
            "fts_cap_applied": _dbg_fts_cap_value,
            # Always emit the actual query passed to the embedder (whether
            # it was the caller's `query` arg or an explicit `embed_query`
            # override for prepending). The flag distinguishes the two cases.
            "embed_query_used": effective_embed_query,
            "embed_query_overridden": embed_query is not None,
            "per_result": per_result_ordered,
            "snapshot_turn": _get_current_turn(),
        }

    return base_result


# ---- Tiered-result helpers ----

def _build_tiered_results(
    results: list[dict],
    summary_top_k: int,
) -> list[dict]:
    """Convert a ranked list of node dicts to the Wave-C tiered return shape.

    Tier 1 (first ``summary_top_k`` entries):
        {"id": "...", "summary": "<recall_summary or truncated claim>"}

    Tier 2 (remaining entries):
        {"id": "...", "keywords": [...]}   if recall_keywords present and non-empty
        {"id": "..."}                       otherwise (bare-ID, intentional)

    Ordering = input ranking order.  No score or position fields.
    Type is carried by the ID prefix (ax_, ob_, dv_, etc.).

    Note: for engram_query_pattern, Tier 2 entries are bare-ID because the
    per-pattern result builders construct dicts from raw SQL without pulling
    recall_keywords.  Future enhancement: update each ``_pattern_*`` function
    to include ``recall_keywords`` for richer Tier 2 rendering on pattern
    queries.

    Args:
        results: Ranked list of node dicts (already processed — metadata stripped,
            neighbors added for engram_query; raw pattern dicts for query_pattern).
        summary_top_k: How many top entries get summaries.  0 = all tier-2;
            >= len(results) = all tier-1.  Caller is responsible for clamping
            to valid range before calling.
    """
    tiered: list[dict] = []
    for i, node in enumerate(results):
        node_id = node.get("id", "")
        confidence = node.get("confidence")
        if i < summary_top_k:
            # Tier 1: prefer recall_summary, fall back to truncated claim.
            summary = node.get("recall_summary") or None
            if not summary:
                claim = node.get("claim") or ""
                summary = (claim[:160] + "...") if len(claim) > 160 else claim
            tiered.append({"id": node_id, "confidence": confidence, "summary": summary})
        else:
            # Tier 2: prefer recall_keywords; bare-ID (+ confidence) if missing/empty.
            kw = node.get("recall_keywords")
            # recall_keywords may arrive as a list (already parsed by _node_to_dict)
            # or as a JSON string if the node came from a pattern impl that does
            # its own dict construction.  Normalise here.
            if isinstance(kw, str):
                try:
                    kw = json.loads(kw)
                except (ValueError, json.JSONDecodeError):
                    kw = None
            if kw:
                tiered.append({"id": node_id, "confidence": confidence, "keywords": kw})
            else:
                tiered.append({"id": node_id, "confidence": confidence})
    return tiered


# ---- Query tools ----


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
def engram_surface(
    query: str,
    top_k: int = 10,
    semantic: bool = True,
    embed_query: str | None = None,
) -> str:
    """Shallow recall: search the KG and return a compact summary nudge.

    This is the "this sounds familiar" layer — a quick scan of what the KG
    knows about a topic. Returns node counts by type, highlights of special
    nodes (axioms, contradictions, open questions, conjectures), top claims,
    and age signals. Does NOT refresh memory on matched nodes.

    Use this to decide whether to dig deeper with engram_inspect (single node
    detail) or engram_get_subgraph (full evidence chain).

    Args:
        query: Natural language search query (used for FTS keyword matching,
            and as the default semantic query when embed_query is None).
        top_k: Maximum nodes to scan (default 10).
        semantic: Whether to include semantic (embedding) search. Set False
            for fast keyword-only recall (useful in hooks where latency
            matters). Default True.
        embed_query: Optional separate semantic-search string. When provided,
            semantic search uses embed_query while FTS still uses query.
            Designed for the auto-surface hook to prepend prev-response-tail
            for short prompts without polluting FTS keyword matching.
            See alpha #177 area 1.

    Returns:
        JSON with compact summary: type counts, special nodes, top claims,
        age range, stale/tainted counts, and matched node IDs for follow-up.
    """
    conn = _get_db()
    try:
        search = _search_nodes(conn, query, top_k=top_k, use_semantic=semantic,
                               embed_query=embed_query)
        results = search["results"]

        if not results:
            return json.dumps({
                "query": query,
                "match_count": 0,
                "message": "No matching nodes found in the knowledge graph.",
            })

        # Snapshot wall-clock now once so every humanized timestamp in this
        # surface response is rendered against the same reading (MECH-2 of
        # dv_0472 / dv_0469 CLOCK-DRIFT hygiene).
        now_utc = datetime.now(timezone.utc)

        # --- Type breakdown ---
        type_counts: dict[str, int] = {}
        for n in results:
            t = n.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        # --- Special nodes (always worth reviewing) ---
        # Covers all SPECIAL_TYPES_BYPASS types: status-bearing (axiom,
        # contradiction, question, conjecture, lesson) and sparse anchor-types
        # (definition, person, goal). All now bypass composite+MMR in
        # _search_nodes, so they can arrive here even when relevance is low.
        # Display cap: top-3 by retrieval order (symmetric with top_claims).
        # Retrieval cap (SPECIAL_POOL_CAP=10) is separate — this is the display
        # slice only.
        special = []
        for n in results:
            ntype = n.get("type", "")
            if ntype in SPECIAL_TYPES_BYPASS:
                entry: dict = {
                    "id": n["id"], "type": ntype,
                    "claim": n.get("claim", ""),
                    "created_ago": _humanized_ago(n.get("created_at"), now=now_utc),
                    "recall_summary": n.get("recall_summary"),
                    "recall_keywords": json.loads(n.get("recall_keywords") or "null"),
                }
                if ntype == "question":
                    entry["status"] = n.get("status", "open")
                elif ntype == "conjecture":
                    entry["status"] = n.get("status", "open")
                    entry["confidence"] = n.get("confidence")
                elif ntype == "axiom":
                    entry["confidence"] = n.get("confidence")
                special.append(entry)
        special = special[:3]

        # --- Top claims (MMR-ordered, non-special) ---
        # Historically this block re-sorted claim_nodes by raw confidence-desc
        # before slicing [:3], which masked the composite+MMR ranked order
        # produced by _search_nodes (ob_1939, alpha #178 area 1).
        # The sort is intentionally removed: _search_nodes already delivers
        # results in composite+MMR order, so claim_nodes[:3] preserves that
        # canonical ranking.  All SPECIAL_TYPES_BYPASS types are excluded here
        # — they are surfaced in special_nodes above.
        claim_nodes = [
            n for n in results
            if n.get("type", "") not in (
                "axiom", "contradiction", "question", "conjecture", "evidence",
                "lesson", "definition", "person", "goal",
            )
            and n.get("confidence") is not None
        ]
        top_claims = [
            {
                "id": n["id"], "type": n["type"],
                "claim": n.get("claim", ""),
                "confidence": n.get("confidence"),
                "created_ago": _humanized_ago(n.get("created_at"), now=now_utc),
                "recall_summary": n.get("recall_summary"),
                "recall_keywords": json.loads(n.get("recall_keywords") or "null"),
            }
            for n in claim_nodes[:3]
        ]

        # --- Age signal ---
        mem = _get_memory_config()
        current_turn = mem.get("current_turn", 0)
        recall_turns = [n.get("recall_turn", 0) for n in results if n.get("recall_turn") is not None]
        old_nodes = [n["id"] for n in results if (n.get("recall_turn") or 0) < max(0, current_turn - 2)]

        # Humanized wall-clock range across the matched set — converts the
        # recall_turn-only age signal (which measures checkpoints, not
        # wall-clock) into a real-time cue, covering T4 (gap-since-last-session)
        # of the dv_0472 temporal-awareness matrix.
        created_ats = [n.get("created_at") for n in results if n.get("created_at")]
        if created_ats:
            newest_iso = max(created_ats)
            oldest_iso = min(created_ats)
            wall_clock_range = {
                "newest_ago": _humanized_ago(newest_iso, now=now_utc),
                "oldest_ago": _humanized_ago(oldest_iso, now=now_utc),
            }
        else:
            wall_clock_range = None

        # --- Stale / tainted counts + structured per-node warnings ---
        # MECH-5: in addition to aggregate counts, surface the full
        # warnings shape per-node so agents can act on specific taints
        # without a follow-up engram_inspect call.
        stale_count = 0
        tainted_count = 0
        warned_nodes: dict[str, dict] = {}
        for n in results:
            meta_str = n.get("metadata", "")
            if isinstance(meta_str, str) and meta_str:
                if '"stale_by"' in meta_str:
                    stale_count += 1
                if '"tainted_by"' in meta_str:
                    tainted_count += 1
            w = _extract_warnings(meta_str, conn)
            if w:
                warned_nodes[n["id"]] = w
        # Attach warnings inline on top_claims / special entries so callers
        # can notice without cross-referencing the warned_nodes map.
        for entry in top_claims:
            if entry["id"] in warned_nodes:
                entry["warnings"] = warned_nodes[entry["id"]]
        for entry in special:
            if entry["id"] in warned_nodes:
                entry["warnings"] = warned_nodes[entry["id"]]

        # --- All matched IDs for follow-up ---
        matched_ids = [n["id"] for n in results]

        # --- Per-node metadata for "Others" digest rendering (Lei 2026-05-19 PM) ---
        # Surfaces id + type + recall_keywords for every matched node so the
        # hook can render a faceted "Others:" list with keyword prefixes for
        # the nodes not in top_claims/special_nodes. Keywords-only (no summary)
        # keeps the lossy-by-design noetic-register principle intact: the agent
        # gets a richer index for "which one should I deliberate-recall on"
        # without flooding context with claim content.
        matched_meta = []
        for n in results:
            kw_raw = n.get("recall_keywords")
            try:
                kw = json.loads(kw_raw) if kw_raw else None
            except (json.JSONDecodeError, TypeError):
                kw = None
            matched_meta.append({
                "id": n["id"],
                "type": n.get("type"),
                "recall_keywords": kw,
            })

        summary = {
            "query": query,
            "match_count": len(results),
            "type_counts": type_counts,
            "special_nodes": special,
            "top_claims": top_claims,
            "age": {
                "current_turn": current_turn,
                "recall_turn_range": [min(recall_turns), max(recall_turns)] if recall_turns else None,
                "wall_clock_range": wall_clock_range,
                "not_recalled_recently": old_nodes,
            },
            "issues": {},
            "matched_ids": matched_ids,
            "matched_meta": matched_meta,
            "search_methods": {
                "keyword_matches": search["keyword_matches"],
                "semantic_matches": search["semantic_matches"],
                "semantic_available": search["semantic_available"],
            },
            "hint": "Use engram_inspect(node_id) for full detail on a specific node, or engram_get_subgraph(node_id) for full evidence chain.",
        }
        if stale_count:
            summary["issues"]["stale_nodes"] = stale_count
        if tainted_count:
            summary["issues"]["tainted_nodes"] = tainted_count
        if warned_nodes:
            summary["issues"]["warnings_by_id"] = warned_nodes

        # --- Tier A/B comparison (ob_0657, cj_0028) ---
        test_config = mem.get("test")
        if test_config and test_config.get("enabled"):
            test_max = test_config.get("tier2_max_nodes", 1000)
            # Compute test threshold using same logic as _get_tier_threshold
            total_current = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
            ).fetchone()["c"]
            if total_current <= test_max:
                test_threshold = 0.0
            else:
                row_t = conn.execute(
                    """SELECT importance_score FROM nodes
                       WHERE is_current = 1
                       ORDER BY importance_score DESC
                       LIMIT 1 OFFSET ?""",
                    (test_max - 1,),
                ).fetchone()
                test_threshold = row_t["importance_score"] if row_t else 0.0

            # Check which base results would be excluded by test config
            tier2_threshold_base = _get_tier_threshold(conn, 2)
            surviving_ids = set()
            excluded = []
            for n in results:
                imp = n.get("importance_score", 0) or 0
                if imp < test_threshold:
                    excluded.append(n)
                else:
                    surviving_ids.add(n["id"])

            # For each excluded node, check 1-hop reachability from surviving nodes
            excluded_details = []
            for n in excluded:
                neighbors = _get_neighbors(conn, n["id"])
                reachable_via = [
                    nb["neighbor_id"] for nb in neighbors
                    if nb["neighbor_id"] in surviving_ids
                ]
                excluded_details.append({
                    "id": n["id"],
                    "type": n.get("type", ""),
                    "claim": (n.get("claim") or "")[:100],
                    "importance_score": round(n.get("importance_score", 0) or 0, 4),
                    "reachable_via": reachable_via[:3],
                })

            reachable_count = sum(1 for e in excluded_details if e["reachable_via"])
            summary["tier_ab"] = {
                "base": {
                    "tier2_max_nodes": mem.get("tier2_max_nodes", 2000),
                    "threshold": round(tier2_threshold_base, 4),
                    "total_searchable": total_current,
                },
                "test": {
                    "tier2_max_nodes": test_max,
                    "threshold": round(test_threshold, 4),
                    "total_searchable": min(total_current, test_max),
                },
                "results_excluded_by_test": len(excluded_details),
                "excluded_but_reachable": reachable_count,
                "excluded_and_orphaned": len(excluded_details) - reachable_count,
                "excluded_details": excluded_details,
                "interpretation": (
                    f"Of {len(results)} results, {len(excluded_details)} would be excluded "
                    f"by test config (tier2={test_max}). "
                    f"{reachable_count} of those are reachable via 1-hop graph links "
                    f"from surviving results (recallable through thinking chains). "
                    f"{len(excluded_details) - reachable_count} would be truly lost."
                ),
            }

        return json.dumps(summary)
    finally:
        conn.close()


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
def engram_inspect(
    node_id: str,
    view: str = "recall",
    dream_mode: bool = False,
) -> str:
    """Inspect a single node with one of three view modes.

    This is the "let me think about that" layer — brings one node into full
    focus with its immediate connections. Refreshes memory on the target node
    only (not neighbors). Use after engram_surface identifies a node of interest.

    For the full evidence chain (multi-hop), use engram_get_subgraph instead.

    Three view modes serve different use cases:

      view="recall" (default):
        "I want to refresh my memory of this idea." Full claim + logical
        neighbors grouped into upstream / downstream / lateral with
        recall_summary, plus contextual neighbors with keywords only.
        Heavy fields (confidence_history, logical_chain, metadata, scores)
        are omitted to keep the response concise.

      view="deep":
        "I need every detail of THIS node for a forensic decision."
        All node fields including confidence_history, logical_chain, metadata,
        importance/utility scores. Neighbors shown as a topology adjacency map
        (per-edge entries with to/relation/direction).

      view="edges":
        "Just show me the connection inventory."
        Node ID + type + topology adjacency map only. Minimal payload for
        quick topology checks.

    Edge classification for recall view:
      Logical-substrate (shown with recall_summary):
        derives_from, supports, supersedes, contradicts, resolves,
        retracts, exemplifies, subtask_of, tensions
      Contextual (shown with keywords only):
        cites, about, serves

    Grouping inside logical-substrate:
      upstream   — inspected node depends on / supersedes / resolves the neighbor
                   (outgoing derives_from, supports, supersedes, resolves, subtask_of)
      downstream — neighbor depends on / supersedes / resolves the inspected node
                   (incoming versions of the upstream relations)
      lateral    — contradicts, tensions, exemplifies, retracts (no clear up/down)

    Args:
        node_id: The node ID to inspect (e.g. 'dv_0010').
        view: One of "recall" (default), "deep", "edges".
        dream_mode: If True, skip memory refresh. Use during dream/audit
            cycles where inspection is maintenance, not informative recall.
            Prevents dream-selected nodes from getting artificially boosted
            importance scores. Default False (normal awake behavior).

    Returns:
        JSON shaped by the chosen view mode (see above).
    """
    _VALID_VIEWS = {"recall", "deep", "edges"}
    if view not in _VALID_VIEWS:
        return json.dumps({
            "error": f"Invalid view '{view}'. Must be one of: recall, deep, edges."
        })

    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        # For edges view, skip the heavy node fetch — just need id + type.
        if view == "edges":
            node_type = row["type"]

            # Refresh + reward still fire for edges view (deliberate probe).
            if not dream_mode:
                _refresh_recall(conn, [node_id])
                _utility_reward(conn, [node_id], action="inspect")
                conn.commit()

            # edges view is pure-topology browsing: the early-return here is intentional —
            # it skips the question-node last_assessed_at bump below (pure-topology calls
            # should not carry assessment side-effects).
            return json.dumps(_build_inspect_edges_view(conn, node_id, node_type))

        # recall + deep: build the full node dict.
        node = _node_to_dict(row)

        # Parse metadata for structured fields (needed by both recall + deep).
        meta = {}
        if node.get("metadata"):
            try:
                meta = json.loads(node["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        node["parsed_metadata"] = meta

        # MECH-2 (dv_0472): render humanized wall-clock ages alongside the
        # ISO timestamps the schema already stores. Additive — the ISO fields
        # stay, so precision isn't lost. `created_ago` is always rendered;
        # the other two only if the source field is present.
        now_utc = datetime.now(timezone.utc)
        node["created_ago"] = _humanized_ago(node.get("created_at"), now=now_utc)
        if node.get("focused_at"):
            node["focused_ago"] = _humanized_ago(node.get("focused_at"), now=now_utc)
        if node.get("last_assessed_at"):
            node["last_assessed_ago"] = _humanized_ago(node.get("last_assessed_at"), now=now_utc)

        # MECH-5: surface taint / stale as a top-level warning.
        warnings = _extract_warnings(meta, conn)

        # Refresh recall on the target node ONLY (skip in dream mode)
        if not dream_mode:
            _refresh_recall(conn, [node_id])
            _utility_reward(conn, [node_id], action="inspect")
            conn.commit()

        # Update assessment timestamp on question nodes (orthogonal to dream_mode)
        if node.get("type") == "question":
            conn.execute(
                "UPDATE nodes SET last_assessed_turn = ?, last_assessed_at = ? WHERE id = ?",
                (_get_current_turn(), _now(), node_id),
            )
            conn.commit()

        if view == "recall":
            return json.dumps(
                _build_inspect_recall_view(conn, node, node_id, dream_mode, warnings)
            )
        else:  # view == "deep"
            return json.dumps(
                _build_inspect_deep_view(conn, node, node_id, dream_mode, warnings)
            )
    finally:
        conn.close()


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
def engram_query(
    query: str,
    types: str = "",
    min_confidence: float = 0.0,
    include_superseded: bool = False,
    top_k: int = 10,
    return_debug: bool = False,
    summary_top_k: int = 3,
) -> str:
    """Search the knowledge graph by natural language query.

    First-class voluntary semantic-recall tool — the autonoetic intentional-search
    complement to engram_surface's ambient/noetic auto-surfacing. Use this when
    you want to recall something specifically ("I know I worked on X before — let
    me actually find it"), not just notice that something might be relevant.

    Two recall surfaces, by design:
      - engram_surface (noetic) — "this sounds familiar"; ambient hints, no refresh.
      - engram_query (autonoetic) — "I want to recall this clearly"; full nodes,
        refreshes recall, strengthens memory.

    For tracing a specific node's chain after either entry point, use
    engram_inspect (single node) or engram_get_subgraph (multi-hop).

    Combines keyword search (FTS5) with semantic similarity (embedding-based)
    for comprehensive retrieval. Keyword results appear first, semantic-only
    results follow — giving you both exact matches and conceptual connections.

    Args:
        query: Natural language search query.
        types: Comma-separated node types to filter (e.g. 'derivation,observation_factual'). Empty = all types.
        min_confidence: Only return nodes at or above this confidence threshold (0.0–1.0).
        include_superseded: If True, include non-current (superseded) nodes. Default False.
        top_k: Maximum results to return (default 10).
        return_debug: If True, return the full legacy shape (all node fields, composite_score,
            similarity, neighbor data, debug key with ranking internals). Default False.
            Intended for SxS comparison and retrieval diagnostics; never bloats normal responses.
        summary_top_k: How many top results get a recall_summary (Tier 1). The remaining
            results get recall_keywords only (Tier 2). Default 3. Clamped to [0, top_k].
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
    """
    conn = _get_db()
    try:
        search = _search_nodes(
            conn, query, types=types, min_confidence=min_confidence,
            include_superseded=include_superseded, top_k=top_k,
            return_debug=return_debug,
        )

        # Enrich results with neighbors (needed for both debug + tiered paths:
        # debug returns the full enriched dicts; tiered path needs recall_keywords
        # which come from _node_to_dict / _search_nodes, but warnings need conn).
        output = []
        for node in search["results"]:
            # MECH-5: extract warnings BEFORE popping metadata so agents
            # reading query results see taint / stale status.
            w = _extract_warnings(node.get("metadata"), conn)
            node.pop("metadata", None)
            node["neighbors"] = _get_neighbors(conn, node["id"])
            if w:
                node["warnings"] = w
            output.append(node)

        # Refresh recall for all accessed nodes.
        if output:
            accessed_ids = [n["id"] for n in output]
            _refresh_recall(conn, accessed_ids)

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # Decision context: query length + result count + type filter used.
        # source_class_filter is not a parameter of engram_query (only type
        # filter exists); emitted as None. Noted in phase-3 F1 handoff.
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_query",
                "query_text_len": len(query),
                "result_count": len(output),
                "type_filter": types or None,
                "source_class_filter": None,
            },
        )

        # return_debug=True: preserve full legacy shape verbatim (eval/harness contract).
        if return_debug:
            response_body: dict = {
                "query": query,
                "results": output,
                "count": len(output),
                "search_methods": {
                    "keyword_matches": search["keyword_matches"],
                    "semantic_matches": search["semantic_matches"],
                    "semantic_available": search["semantic_available"],
                },
                "message": (
                    f"Found {len(output)} matching node(s)."
                    if output
                    else "No matching nodes found in the knowledge graph."
                ),
            }
            if "debug" in search:
                response_body["debug"] = search["debug"]
            return json.dumps(response_body)

        # Default path: tiered summary/keywords shape (Wave C recall_summary rollout).
        # Clamp summary_top_k to [0, len(output)] so callers don't have to worry
        # about off-by-one edge cases.
        effective_top_k = max(0, min(summary_top_k, len(output)))
        tiered = _build_tiered_results(output, effective_top_k)
        return json.dumps({
            "results": tiered,
            "query": query,
            "total_matches": len(output),
        })
    finally:
        conn.close()


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
def engram_get_subgraph(
    node_id: str,
    depth: int = 2,
    direction: str = "both",
    view: str = "recall",
    dream_mode: bool = False,
) -> str:
    """Browse a node's connection topology within N hops.

    This is a BROWSING tool, not a content tool.  The caller is assumed to
    already know the root node's content (from engram_inspect or a prior
    query); subgraph shows the connection topology plus just-enough content
    to recognise which branch is worth following next.  When a node in the
    subgraph looks interesting, the caller does a fresh engram_inspect on
    that node, then optionally calls engram_get_subgraph again from the new
    root — the intended chained-call pattern.

    Two views:

    * ``recall`` (default) — topology + hop-graduated content summaries.
      Root and 1-hop neighbours include ``recall_summary`` + ``recall_keywords``.
      2+-hop neighbours include only ``recall_keywords`` (lighter — you need a
      hint, not a read).  Each content entry also carries ``hop_distance`` so
      the caller knows which depth the node sits at.

    * ``edges`` — topology only (no ``content`` key).  Useful when you want
      the pure connectivity map without any textual weight.

    Hop-graduated lightness rule:
      hop_distance=0 (root)   → type + recall_summary + recall_keywords
      hop_distance=1          → type + recall_summary + recall_keywords
      hop_distance=2+         → type + recall_keywords only (no recall_summary)

    The topology dict is keyed by source node ID; each value is a list of
    ``{to, relation, direction}`` entries.  Only edges where both endpoints
    are in the subgraph are included — boundary edges to unseen nodes are
    dropped.

    Args:
        node_id: Root node to explore (e.g. 'dv_0686').
        depth: How many hops to traverse from the root (default 2).
        direction: 'up' (toward evidence/sources), 'down' (toward derivations
            that cite this), or 'both' (default).
        view: 'recall' (default, topology + hop-graduated summaries) or
            'edges' (topology only).  Passing 'deep' is rejected — use
            engram_inspect(node_id, view='deep') instead.
        dream_mode: If True, skip recall_refresh on neighbour nodes (useful
            during dream/audit cycles).  The root node always receives a
            utility bump regardless of dream_mode — it is the agent's explicit
            USE target.  Default False.

    Returns:
        JSON with topology (adjacency map keyed by node ID) and, for
        view='recall', a content dict with per-node summaries.
    """
    if view not in ("recall", "edges"):
        if view == "deep":
            return json.dumps({
                "error": (
                    "view='deep' is not supported by engram_get_subgraph. "
                    "Use engram_inspect(node_id, view='deep') for full node content."
                )
            })
        return json.dumps({
            "error": f"Invalid view '{view}'. Valid values: 'recall', 'edges'."
        })

    if direction not in ("up", "down", "both"):
        return json.dumps({
            "error": f"Invalid direction '{direction}'. Valid values: 'up', 'down', 'both'."
        })

    conn = _get_db()
    try:
        root_row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not root_row:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        # BFS traversal — track hop_distance per node.
        # visited maps node_id → hop_distance
        visited: dict[str, int] = {}
        edges_out: list[dict] = []
        from collections import deque

        queue: deque[tuple[str, int]] = deque()
        queue.append((node_id, 0))

        while queue:
            nid, hop = queue.popleft()
            if nid in visited:
                continue
            visited[nid] = hop

            if hop >= depth:
                # At the depth boundary — record the node but don't traverse further
                continue

            # Traverse edges based on direction
            if direction in ("up", "both"):
                # Outgoing edges from this node (toward evidence/sources)
                outgoing = conn.execute(
                    "SELECT * FROM edges WHERE source_id = ?", (nid,)
                ).fetchall()
                for e in outgoing:
                    edges_out.append(dict(e))
                    if e["target_id"] not in visited:
                        queue.append((e["target_id"], hop + 1))

            if direction in ("down", "both"):
                # Incoming edges to this node (toward dependents)
                incoming = conn.execute(
                    "SELECT * FROM edges WHERE target_id = ?", (nid,)
                ).fetchall()
                for e in incoming:
                    edges_out.append(dict(e))
                    if e["source_id"] not in visited:
                        queue.append((e["source_id"], hop + 1))

        # Deduplicate edges (source_id, target_id, relation) and keep only those
        # where BOTH endpoints are in the subgraph (topology truncation rule).
        seen_edge_keys: set[tuple] = set()
        unique_edges: list[dict] = []
        for e in edges_out:
            src, tgt, rel = e["source_id"], e["target_id"], e["relation"]
            key = (src, tgt, rel)
            if key in seen_edge_keys:
                continue
            # Drop boundary edges whose far endpoint is outside the subgraph
            if src not in visited or tgt not in visited:
                continue
            seen_edge_keys.add(key)
            edge_entry: dict = {"source_id": src, "target_id": tgt, "relation": rel}
            unique_edges.append(edge_entry)

        # Build adjacency-map topology: keyed by source node ID.
        # Each entry: {to, relation, direction (outgoing/incoming relative to key node)}
        topology: dict[str, list[dict]] = {nid: [] for nid in visited}
        for e in unique_edges:
            src, tgt, rel = e["source_id"], e["target_id"], e["relation"]
            entry_out: dict = {"to": tgt, "relation": rel, "direction": "outgoing"}
            entry_in: dict = {"to": src, "relation": rel, "direction": "incoming"}
            topology[src].append(entry_out)
            topology[tgt].append(entry_in)

        # Build content dict for view="recall"
        content: Optional[dict] = None
        if view == "recall":
            content = {}
            for nid, hop in visited.items():
                node_row = conn.execute(
                    "SELECT type, recall_summary, recall_keywords FROM nodes WHERE id = ?",
                    (nid,),
                ).fetchone()
                if not node_row:
                    continue
                entry: dict = {
                    "type": node_row["type"],
                    "hop_distance": hop,
                }
                kw_raw = node_row["recall_keywords"]
                kw = None
                if kw_raw:
                    try:
                        kw = json.loads(kw_raw)
                    except (json.JSONDecodeError, TypeError):
                        kw = None
                if kw is not None:
                    entry["recall_keywords"] = kw
                # recall_summary: root + 1-hop only; suppress at 2+
                if hop <= 1:
                    rs = node_row["recall_summary"]
                    if rs:
                        entry["recall_summary"] = rs
                content[nid] = entry

        # Memory management
        if visited:
            # Utility bump: root only (the agent's explicit USE target)
            _utility_reward(conn, [node_id], action="subgraph")
            # Recall refresh: all neighbours except root, unless dream_mode
            if not dream_mode:
                neighbor_ids = [nid for nid in visited if nid != node_id]
                if neighbor_ids:
                    _refresh_recall(conn, neighbor_ids)
            conn.commit()

        response: dict = {
            "root": node_id,
            "depth": depth,
            "direction": direction,
            "view": view,
            "dream_mode": dream_mode,
            "topology": topology,
            "node_count": len(visited),
            "edge_count": len(unique_edges),
        }
        if content is not None:
            response["content"] = content

        return json.dumps(response)
    finally:
        conn.close()


# ---- Version control tools ----


def _checkpoint_internal(message: str, advance_turn: bool) -> str:
    """Internal checkpoint logic shared by engram_nap and engram_advance_turn.

    Computes graph stats, appends to session_log, snapshots + commits, stores
    a diagnostic snapshot. If advance_turn is True, increments the global
    turn counter (driving the forgetting mechanism). If False, leaves the
    turn counter alone (nap mode) and arms a nap_checkpoint feeling nudge.

    The two public tools (engram_nap and engram_advance_turn) exist as
    distinct MCP entry points so a missed kwarg cannot accidentally promote
    a nap into a turn-advancing checkpoint — the prior single-tool design
    was bug-prone under context pressure (heartbeat sessions especially).
    """
    mode = "session" if advance_turn else "nap"
    mem = _get_memory_config()
    old_turn = mem.get("current_turn", 0)

    if advance_turn:
        new_turn = old_turn + 1
        _set_current_turn(new_turn)
    else:
        new_turn = old_turn  # no advancement

    conn = _get_db()
    try:
        # Collect overall stats
        node_count = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        edge_count = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        current_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
        ).fetchone()["c"]

        type_counts = {}
        for row in conn.execute(
            "SELECT type, COUNT(*) as c FROM nodes WHERE is_current = 1 GROUP BY type"
        ).fetchall():
            type_counts[row["type"]] = row["c"]

        # What was added this turn (nodes created at the current turn)
        new_nodes = conn.execute(
            "SELECT id, type, claim FROM nodes WHERE recall_turn = ? ORDER BY id",
            (old_turn,),
        ).fetchall()
        new_node_summary = [
            {"id": r["id"], "type": r["type"], "claim": (r["claim"] or "")[:80]}
            for r in new_nodes
        ]

        # Memory tier stats
        t1 = _get_tier_threshold(conn, 1)
        t2 = _get_tier_threshold(conn, 2)
        tier1_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t1,),
        ).fetchone()["c"] if t1 > 0 else current_count
        tier2_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t2,),
        ).fetchone()["c"] if t2 > 0 else current_count

        # Append to session log
        now = _now()
        new_nodes_text = "\n".join(
            f"  - {n['id']} ({n['type']}): {n['claim']}" for n in new_node_summary
        ) if new_node_summary else "  (no new nodes)"

        mode_label = f"Turn {new_turn}" if mode != "nap" else f"Nap (turn {old_turn})"
        log_entry = (
            f"\n## {mode_label} — {now[:19]}\n\n"
            f"{message}\n\n"
            f"**New nodes ({len(new_node_summary)}):**\n{new_nodes_text}\n\n"
            f"**Graph totals:** {current_count} current nodes ({node_count} total), {edge_count} edges\n"
            f"**By type:** {', '.join(f'{t}: {c}' for t, c in sorted(type_counts.items()))}\n"
            f"**Memory tiers:** working={tier1_count}, searchable={tier2_count}, total={current_count}\n"
            f"\n---\n"
        )

        # Create or append to log file
        if not LOG_PATH.exists():
            LOG_PATH.write_text("# KG Memory Session Log\n\n---\n", encoding="utf-8")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(log_entry)

        # Snapshot + git commit (durable version-controlled record + restore point)
        # Best-effort: never raises. If git is unavailable, the checkpoint still
        # succeeds and the version_control field reports why.
        version_control = _commit_snapshot(conn, message, mode)

        result = {
            "status": "checkpoint_saved",
            "mode": mode,
            "previous_turn": old_turn,
            "current_turn": new_turn,
            "turn_advanced": mode != "nap",
            "message": message,
            "log_path": str(LOG_PATH),
            "new_nodes_this_session": len(new_node_summary),
            "graph_stats": {
                "total_nodes": node_count,
                "current_nodes": current_count,
                "total_edges": edge_count,
                "by_type": type_counts,
            },
            "memory_tiers": {
                "tier1_working_memory": tier1_count,
                "tier2_searchable": tier2_count,
                "tier3_total": current_count,
                "decay_base": mem.get("decay_base", 1.014),
            },
            "version_control": version_control,
        }

        # Store diagnostic snapshot (best-effort, never blocks checkpoint)
        try:
            diag_metrics = json.loads(engram_diagnose())
            conn2 = _get_db()
            try:
                conn2.execute(
                    """INSERT INTO diagnostic_history (timestamp, turn, checkpoint_mode, metrics)
                       VALUES (?, ?, ?, ?)""",
                    (now, new_turn, mode, json.dumps(diag_metrics)),
                )
                conn2.commit()
                result["diagnostic_snapshot"] = {
                    "stored": True,
                    "health_score": diag_metrics.get("health_score"),
                }
            finally:
                conn2.close()
        except Exception:
            result["diagnostic_snapshot"] = {"stored": False, "reason": "diagnostic computation failed"}

        # Nap-cycle feeling-report nudge (§6 Marker file protocol). After
        # session-end review the agent is already in a reflective posture,
        # so this is a low-cost capture point. The marker is written BEFORE
        # returning so any subsequent engram_report_feeling within TTL gets
        # auto-tagged `nap_checkpoint`. Null results are explicitly valid.
        if mode == "nap":
            result["feeling_report_nudge"] = (
                "Reviewing this session's work, did any specific moment "
                "produce a distinct internal state worth marking — surprise, "
                "recognition, dissonance, unusual satisfaction, discomfort "
                "that isn't task-friction? If yes, file via "
                "engram_report_feeling — it will be auto-tagged "
                "nap_checkpoint. If no, carry on; a null result is a valid "
                "data point."
            )
            _write_feeling_nudge("nap_checkpoint")

        return json.dumps(result)
    finally:
        conn.close()


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
def engram_nap(message: str) -> str:
    """Persist context to ENGRAM without advancing the turn counter.

    Use BEFORE compaction, at the end of a work-burst, or whenever you want
    to preserve session knowledge to the durable graph without triggering
    forgetting. Logs the summary to session_log, snapshots + commits the
    graph, stores a diagnostic snapshot, and arms a nap_checkpoint feeling-
    report nudge (TTL 5 turns). Does NOT advance the global turn counter —
    the forgetting mechanism stays paused.

    For end-of-day session checkpoints (sleep cycle), use engram_advance_turn
    instead. The two functions exist as distinct tools so a missed kwarg
    cannot accidentally promote a nap into a turn-advancing checkpoint.

    Args:
        message: Summary of what was learned/changed in this work-burst.

    Returns:
        JSON with previous_turn (unchanged from current_turn since no
        advance), changes summary, graph stats, and memory tiers.
    """
    return _checkpoint_internal(message, advance_turn=False)


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
def engram_advance_turn(message: str) -> str:
    """[Sleep cycle / first session only — DO NOT call awake.] End-of-day
    session checkpoint that advances the turn counter + logs summary.

    For pre-compaction / end-of-burst persistence in awake state, use
    engram_nap instead — it persists without consuming the day's turn.

    Advances the global turn counter (drives the forgetting mechanism that
    inflates importance scores of fresh nodes relative to stale ones), logs
    the session summary to session_log, snapshots + commits the graph, and
    records a diagnostic snapshot. **This action is IRREVERSIBLE** — turn
    advances cannot be undone without direct DB intervention.

    Coordinated multi-role discipline (per qu_0035): only the dedicated sleep
    cron at end-of-day should call this from cron-fired sessions. Heartbeat
    / telegram / dispatcher roles must use engram_nap instead. The user's
    explicit sleep skill invocation also overrides cron coordination — but
    state explicitly that the day's turn is being consumed so the cron knows
    to skip its session-mode call.

    Args:
        message: Summary of what was learned/changed across the cohort
                 being closed by this turn advance.

    Returns:
        JSON with new turn number, changes summary, graph stats, and memory
        tiers.
    """
    return _checkpoint_internal(message, advance_turn=True)


# ---- Bonus: stats tool (lightweight, useful even in MVP) ----


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

    Single-payload signature: pass ALL options as one JSON object string in
    `payload_json`. Default `{}` preserves backward compat (no-arg callers
    continue to work unchanged — existing callers pass no payload at all,
    which triggers the `="{}"` default).

    Mode windowing: `mode` restricts all sections to nodes created within
    a time window approximated from created_at timestamps. Because ENGRAM
    does not record turn-advance timestamps (deferred to a future PR), each
    turn-unit maps to 24h. This is accurate in steady-state where the dream
    cycle advances the turn ~once per day, but drifts during high-velocity
    or low-velocity periods. Future tightening can record last_turn_advance
    in config.json and use that boundary directly.

    Args:
        payload_json: JSON object (as a string) with these optional fields:
            mode (str, optional): Window filter — one of "all" (default),
                "1-turn" (≈24h), "7-turn" (≈168h), "30-turn" (≈720h).
                When mode != "all", all sections reflect only nodes created
                within the window. A `window` key is added to the response
                describing the cutoff.
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
                source_class. New in this version; always included unless
                sections selector excludes it.
            window (conditional): present only when mode != "all"; describes
                the time window applied.
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

    # Section name → response key(s) mapping.
    # "health_score" is a recognized name but engram_stats doesn't compute
    # it (that's engram_diagnose's domain); including it in sections is a
    # no-op that doesn't error.
    _VALID_SECTIONS = {
        "structure", "edges", "confidence", "open_questions",
        "open_predictions", "reasoning_breakdown", "weakest_nodes",
        "health_score", "memory",
    }
    if sections_param is not None:
        if not isinstance(sections_param, list):
            return json.dumps({"error": "sections must be a list of strings"})
        if not all(isinstance(s, str) for s in sections_param):
            return json.dumps({"error": "sections must be a list of strings"})
        unknown = [s for s in sections_param if s not in _VALID_SECTIONS]
        if unknown:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "engram_stats: unknown section name(s) ignored: %s", unknown
            )
        wanted = {s for s in sections_param if s in _VALID_SECTIONS}
    else:
        wanted = None  # None = all sections

    def _want(section: str) -> bool:
        return wanted is None or section in wanted

    # ── Build window filter ────────────────────────────────────────────────
    _MODE_HOURS = {"1-turn": 24, "7-turn": 168, "30-turn": 720}
    now_utc = datetime.now(timezone.utc)
    window_meta: Optional[dict] = None
    created_at_filter = ""   # empty string = no filter (mode="all")
    filter_args: list = []

    if mode != "all":
        hours = _MODE_HOURS[mode]
        cutoff_dt = now_utc - timedelta(hours=hours)
        cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        created_at_filter = " AND created_at >= ?"
        filter_args = [cutoff_iso]
        window_meta = {
            "mode": mode,
            "approx_hours": hours,
            "cutoff_iso": cutoff_iso,
        }

    conn = _get_db()
    try:
        result: dict = {}

        # Attach window metadata first so callers see it regardless of sections.
        if window_meta is not None:
            result["window"] = window_meta

        # ── structure ──────────────────────────────────────────────────────
        if _want("structure"):
            type_counts = {}
            for row in conn.execute(
                "SELECT type, COUNT(*) as c, SUM(is_current) as current_c"
                " FROM nodes WHERE 1=1" + created_at_filter + " GROUP BY type",
                filter_args,
            ).fetchall():
                type_counts[row["type"]] = {
                    "total": row["c"],
                    "current": row["current_c"],
                }
            result["node_counts_by_type"] = type_counts

        # ── edges ──────────────────────────────────────────────────────────
        if _want("edges"):
            if mode == "all":
                edge_rows = conn.execute(
                    "SELECT relation, COUNT(*) as c FROM edges GROUP BY relation"
                ).fetchall()
            else:
                # edges table has its own created_at column — use it directly.
                edge_rows = conn.execute(
                    "SELECT relation, COUNT(*) as c FROM edges"
                    " WHERE created_at >= ? GROUP BY relation",
                    filter_args,
                ).fetchall()
            edge_counts = {row["relation"]: row["c"] for row in edge_rows}
            result["edge_counts_by_relation"] = edge_counts

        # ── open_questions ─────────────────────────────────────────────────
        if _want("open_questions"):
            # Open / partially-resolved questions: count + top 5 most-recent
            # highlights. The full enumeration belongs to engram_reflect; bulk-
            # listing all of them here ballooned the response past the MCP
            # token budget when the graph accumulated >100 open questions
            # (ob_1473 cohort, observed 2026-05-03 — 111K bytes, harness fell
            # back to file redirection). Pattern matches weakest_nodes (top 5).
            open_question_count = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = 'question'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter,
                filter_args,
            ).fetchone()["c"]
            open_questions = conn.execute(
                "SELECT id, claim, status, question_category, question_lacks"
                " FROM nodes WHERE type = 'question'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter
                + " ORDER BY created_at DESC LIMIT 5",
                filter_args,
            ).fetchall()
            result["open_questions_count"] = open_question_count
            result["open_questions_recent"] = [
                {k: v for k, v in {
                    "id": q["id"], "question": q["claim"], "status": q["status"],
                    "category": q["question_category"], "lacks": q["question_lacks"],
                }.items() if v is not None}
                for q in open_questions
            ]

        # ── open_predictions ───────────────────────────────────────────────
        if _want("open_predictions"):
            # Open / partially-resolved predictions: count + top 5 most-recent.
            open_prediction_count = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = 'prediction'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter,
                filter_args,
            ).fetchone()["c"]
            open_predictions = conn.execute(
                "SELECT id, predicted_event, resolution_timeframe, status"
                " FROM nodes WHERE type = 'prediction'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter
                + " ORDER BY created_at DESC LIMIT 5",
                filter_args,
            ).fetchall()
            result["open_predictions_count"] = open_prediction_count
            result["open_predictions_recent"] = [
                {
                    "id": p["id"],
                    "event": p["predicted_event"],
                    "timeframe": p["resolution_timeframe"],
                    "status": p["status"],
                }
                for p in open_predictions
            ]

        # ── reasoning_breakdown ────────────────────────────────────────────
        if _want("reasoning_breakdown"):
            reasoning_breakdown: dict = {}
            derivations = conn.execute(
                "SELECT metadata FROM nodes WHERE type = 'derivation'"
                " AND is_current = 1 AND metadata IS NOT NULL"
                + created_at_filter,
                filter_args,
            ).fetchall()
            for d in derivations:
                try:
                    meta = json.loads(d["metadata"])
                    rtype = meta.get("reasoning_type", "legacy_untyped")
                except (json.JSONDecodeError, TypeError):
                    rtype = "legacy_untyped"
                reasoning_breakdown[rtype] = reasoning_breakdown.get(rtype, 0) + 1
            result["reasoning_type_breakdown"] = reasoning_breakdown

        # ── weakest_nodes ──────────────────────────────────────────────────
        if _want("weakest_nodes"):
            weak_nodes = conn.execute(
                "SELECT id, type, claim, confidence FROM nodes"
                " WHERE is_current = 1 AND confidence IS NOT NULL AND type != 'evidence'"
                + created_at_filter
                + " ORDER BY confidence ASC LIMIT 5",
                filter_args,
            ).fetchall()
            result["weakest_nodes"] = [
                {
                    "id": w["id"],
                    "type": w["type"],
                    "claim": w["claim"],
                    "confidence": w["confidence"],
                }
                for w in weak_nodes
            ]

        # ── memory ─────────────────────────────────────────────────────────
        if _want("memory"):
            result["memory"] = {
                "current_turn": _get_current_turn(),
                "tier1_threshold": _get_tier_threshold(conn, 1),
                "tier2_threshold": _get_tier_threshold(conn, 2),
            }

        # ── confidence ─────────────────────────────────────────────────────
        if _want("confidence"):
            result["confidence"] = _compute_confidence_distribution(
                conn, created_at_filter, filter_args
            )

        return json.dumps(result)
    finally:
        conn.close()


def _compute_confidence_distribution(
    conn: sqlite3.Connection,
    created_at_filter: str,
    filter_args: list,
) -> dict:
    """Compute the confidence distribution section for engram_stats.

    Returns a dict with by_type, by_quote_type, by_reasoning_type,
    by_source_class sub-keys. Empty buckets are omitted. Quantiles are
    computed via Python after fetching confidence values (simpler than
    SQLite window functions, and the dataset is small enough that the
    extra round-trip is negligible).

    Status filter applied: is_current=1 AND (status IS NULL OR status != 'retracted').
    The created_at_filter / filter_args are passed through from the caller
    to respect the mode window.
    """
    # Common status + current filter
    _base_filter = (
        " AND is_current = 1"
        " AND (status IS NULL OR status != 'retracted')"
    )

    # ── by_type: full quantile stats ──────────────────────────────────────
    _CONFIDENCE_BEARING_TYPES = (
        "observation_factual",
        "derivation",
        "conjecture",
        "lesson",
        "axiom",
        "definition",
    )
    by_type: dict = {}
    for ntype in _CONFIDENCE_BEARING_TYPES:
        rows = conn.execute(
            "SELECT confidence FROM nodes WHERE type = ?"
            + _base_filter
            + created_at_filter
            + " AND confidence IS NOT NULL ORDER BY confidence ASC",
            [ntype] + filter_args,
        ).fetchall()
        confidences = [r["confidence"] for r in rows]
        if not confidences:
            continue
        n = len(confidences)
        mean_val = round(sum(confidences) / n, 3)
        by_type[ntype] = {
            "n": n,
            "mean": mean_val,
            "p25": round(_percentile(confidences, 25), 2),
            "p50": round(_percentile(confidences, 50), 2),
            "p75": round(_percentile(confidences, 75), 2),
            "p90": round(_percentile(confidences, 90), 2),
            "p95": round(_percentile(confidences, 95), 2),
        }

    # ── by_quote_type: mean only, restricted to observation_factual ────────
    by_quote_type: dict = {}
    for row in conn.execute(
        "SELECT quote_type, COUNT(*) as n, AVG(confidence) as mean_conf"
        " FROM nodes WHERE type = 'observation_factual'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND quote_type IS NOT NULL"
        " GROUP BY quote_type",
        filter_args,
    ).fetchall():
        qt = row["quote_type"]
        if not qt:
            continue
        by_quote_type[qt] = {
            "n": row["n"],
            "mean": round(row["mean_conf"], 3),
        }

    # ── by_reasoning_type: mean only, restricted to derivation ─────────────
    # reasoning_type lives in metadata JSON; fetch all derivations and
    # aggregate in Python to avoid fragile json_extract across SQLite versions.
    by_reasoning_type: dict = {}
    deriv_rows = conn.execute(
        "SELECT confidence, metadata FROM nodes WHERE type = 'derivation'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND metadata IS NOT NULL",
        filter_args,
    ).fetchall()
    _rt_buckets: dict = {}  # reasoning_type → list of confidence values
    for row in deriv_rows:
        try:
            meta = json.loads(row["metadata"])
            rtype = meta.get("reasoning_type")
        except (json.JSONDecodeError, TypeError):
            rtype = None
        if not rtype:
            rtype = "legacy_untyped"
        _rt_buckets.setdefault(rtype, []).append(row["confidence"])
    for rtype, vals in _rt_buckets.items():
        by_reasoning_type[rtype] = {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
        }

    # ── by_source_class: mean only, restricted to observation_factual ──────
    # source_class lives in metadata JSON; aggregate in Python.
    by_source_class: dict = {}
    obs_rows = conn.execute(
        "SELECT confidence, metadata FROM nodes WHERE type = 'observation_factual'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND metadata IS NOT NULL",
        filter_args,
    ).fetchall()
    _sc_buckets: dict = {}
    for row in obs_rows:
        try:
            meta = json.loads(row["metadata"])
            sc = meta.get("source_class", "external")
        except (json.JSONDecodeError, TypeError):
            sc = "external"
        _sc_buckets.setdefault(sc, []).append(row["confidence"])
    for sc, vals in _sc_buckets.items():
        by_source_class[sc] = {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
        }

    return {
        "by_type": by_type,
        "by_quote_type": by_quote_type,
        "by_reasoning_type": by_reasoning_type,
        "by_source_class": by_source_class,
    }


def _percentile(sorted_vals: list, p: float) -> float:
    """Compute the p-th percentile of a pre-sorted list of floats.

    Uses linear interpolation between the two nearest ranks (same as
    numpy's `percentile` with method='linear'). If the list has one
    element, returns that element regardless of p.

    Tie handling: when multiple values are identical at a percentile
    boundary, interpolation still applies to the two boundary values,
    which happen to be equal — so the result is unambiguous.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    # Map p ∈ [0, 100] to an index in [0, n-1]
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


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
    Designed for both user and agent consumption. Returns machine-readable
    metrics across six dimensions: graph structure, epistemic health, memory
    health, provenance, agent experience, and calibration.

    Unlike engram_reflect (pre-dream briefing, action-oriented), this tool
    produces exhaustive quantitative metrics suitable for trend analysis and
    operational health monitoring.

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
    """
    conn = _get_db()
    try:
        metrics: dict = {}

        # ── Graph Structure ──────────────────────────────────────────────
        structure: dict = {}

        # Node counts by type and currency
        type_rows = conn.execute(
            """SELECT type,
                      COUNT(*) as total,
                      SUM(is_current) as current_count,
                      SUM(CASE WHEN is_current = 0 THEN 1 ELSE 0 END) as superseded_count
               FROM nodes GROUP BY type"""
        ).fetchall()
        type_counts = {}
        total_nodes = 0
        total_current = 0
        for r in type_rows:
            type_counts[r["type"]] = {
                "total": r["total"],
                "current": r["current_count"],
                "superseded": r["superseded_count"],
            }
            total_nodes += r["total"]
            total_current += r["current_count"]
        structure["node_counts"] = type_counts
        structure["total_nodes"] = total_nodes
        structure["total_current"] = total_current

        # Edge counts by relation
        edge_rows = conn.execute(
            "SELECT relation, COUNT(*) as c FROM edges GROUP BY relation"
        ).fetchall()
        edge_counts = {r["relation"]: r["c"] for r in edge_rows}
        total_edges = sum(edge_counts.values())
        structure["edge_counts"] = edge_counts
        structure["total_edges"] = total_edges

        # DAG violations
        exempt_placeholders = ",".join("?" * len(DAG_EXEMPT_RELATIONS))
        dag_violations = conn.execute(
            f"""SELECT COUNT(*) as c FROM edges e
                JOIN nodes s ON e.source_id = s.id
                JOIN nodes t ON e.target_id = t.id
                WHERE s.created_at < t.created_at
                  AND e.relation NOT IN ({exempt_placeholders})""",
            tuple(DAG_EXEMPT_RELATIONS),
        ).fetchone()["c"]
        structure["dag_violations"] = dag_violations

        # Orphan nodes (no edges at all)
        orphans = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.is_current = 1
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.source_id = n.id)
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = n.id)
        """).fetchone()["c"]
        structure["orphan_nodes"] = orphans

        # Average in/out degree (current nodes only)
        if total_current > 0:
            out_degree = conn.execute("""
                SELECT AVG(deg) as avg_deg FROM (
                    SELECT COUNT(*) as deg FROM edges e
                    JOIN nodes n ON e.source_id = n.id
                    WHERE n.is_current = 1
                    GROUP BY e.source_id
                )
            """).fetchone()["avg_deg"] or 0
            in_degree = conn.execute("""
                SELECT AVG(deg) as avg_deg FROM (
                    SELECT COUNT(*) as deg FROM edges e
                    JOIN nodes n ON e.target_id = n.id
                    WHERE n.is_current = 1
                    GROUP BY e.target_id
                )
            """).fetchone()["avg_deg"] or 0
            structure["avg_out_degree"] = round(out_degree, 2)
            structure["avg_in_degree"] = round(in_degree, 2)
        else:
            structure["avg_out_degree"] = 0
            structure["avg_in_degree"] = 0

        # Dangling evidence (evidence with no observations)
        dangling_ev = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.type = 'evidence' AND n.is_current = 1
              AND NOT EXISTS (
                  SELECT 1 FROM nodes o
                  WHERE o.evidence_id = n.id AND o.is_current = 1
              )
        """).fetchone()["c"]
        structure["dangling_evidence"] = dangling_ev

        metrics["structure"] = structure

        # ── Epistemic Health ─────────────────────────────────────────────
        epistemic: dict = {}

        # Confidence distribution (buckets)
        conf_buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0,
                        "0.6-0.8": 0, "0.8-1.0": 0}
        conf_rows = conn.execute(
            """SELECT confidence FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL"""
        ).fetchall()
        for cr in conf_rows:
            c = cr["confidence"]
            if c < 0.2:
                conf_buckets["0.0-0.2"] += 1
            elif c < 0.4:
                conf_buckets["0.2-0.4"] += 1
            elif c < 0.6:
                conf_buckets["0.4-0.6"] += 1
            elif c < 0.8:
                conf_buckets["0.6-0.8"] += 1
            else:
                conf_buckets["0.8-1.0"] += 1
        epistemic["confidence_distribution"] = conf_buckets

        # Mean and median confidence
        if conf_rows:
            confs = sorted(cr["confidence"] for cr in conf_rows)
            epistemic["confidence_mean"] = round(sum(confs) / len(confs), 3)
            mid = len(confs) // 2
            epistemic["confidence_median"] = confs[mid] if len(confs) % 2 else round((confs[mid - 1] + confs[mid]) / 2, 3)
        else:
            epistemic["confidence_mean"] = 0
            epistemic["confidence_median"] = 0

        # Weakly grounded (conf < 0.5, claim-bearing types)
        weakly_grounded = conn.execute(
            """SELECT COUNT(*) as c FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL
               AND confidence < 0.5
               AND type NOT IN ('evidence', 'contradiction', 'question',
                                'definition', 'goal', 'goal_tension',
                                'feeling_report', 'prediction')"""
        ).fetchone()["c"]
        epistemic["weakly_grounded"] = weakly_grounded

        # Single-source derivations
        single_source_dvs = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.type = 'derivation' AND n.is_current = 1
              AND (SELECT COUNT(*) FROM edges e
                   WHERE e.source_id = n.id AND e.relation = 'derives_from') <= 1
        """).fetchone()["c"]
        epistemic["single_source_derivations"] = single_source_dvs

        # Tainted nodes
        tainted_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND metadata LIKE '%\"tainted_by\"%'"
        ).fetchone()["c"]
        epistemic["tainted_nodes"] = tainted_count

        # Stale nodes
        stale_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND metadata LIKE '%\"stale_by\"%'"
        ).fetchone()["c"]
        epistemic["stale_nodes"] = stale_count

        # Retracted nodes
        retracted_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE status = 'retracted'"
        ).fetchone()["c"]
        epistemic["retracted_nodes"] = retracted_count

        # Uncited observations (not referenced by any derivation)
        uncited = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.type IN ('observation_factual', 'observation_predictive')
              AND n.is_current = 1
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  WHERE e.target_id = n.id
                    AND e.relation IN ('derives_from', 'supports')
                    AND e.source_id IN (SELECT id FROM nodes WHERE type IN ('derivation', 'theory'))
              )
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  WHERE e.source_id = n.id AND e.relation = 'supports'
              )
        """).fetchone()["c"]
        epistemic["uncited_observations"] = uncited

        # Resolution rates
        def _resolution_rate(node_type, resolved_statuses=("resolved",)):
            total = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = ?", (node_type,)
            ).fetchone()["c"]
            if total == 0:
                return {"total": 0, "resolved": 0, "rate": 0}
            resolved = conn.execute(
                f"SELECT COUNT(*) as c FROM nodes WHERE type = ? AND status IN ({','.join('?' * len(resolved_statuses))})",
                (node_type, *resolved_statuses),
            ).fetchone()["c"]
            return {"total": total, "resolved": resolved,
                    "rate": round(resolved / total, 3)}

        epistemic["contradiction_resolution"] = _resolution_rate(
            "contradiction", ("resolved", "partially_resolved"))
        epistemic["question_resolution"] = _resolution_rate(
            "question", ("resolved", "partially_resolved"))
        epistemic["conjecture_resolution"] = _resolution_rate(
            "conjecture", ("resolved", "supported", "refuted"))
        epistemic["prediction_resolution"] = _resolution_rate(
            "prediction", ("confirmed", "disconfirmed", "resolved", "partially_resolved"))

        # Reasoning type breakdown for derivations
        reasoning_breakdown = {}
        dvs = conn.execute(
            "SELECT metadata FROM nodes WHERE type = 'derivation' AND is_current = 1 AND metadata IS NOT NULL"
        ).fetchall()
        for d in dvs:
            try:
                meta = json.loads(d["metadata"])
                rtype = meta.get("reasoning_type", "legacy_untyped")
            except (json.JSONDecodeError, TypeError):
                rtype = "legacy_untyped"
            reasoning_breakdown[rtype] = reasoning_breakdown.get(rtype, 0) + 1
        epistemic["reasoning_types"] = reasoning_breakdown

        metrics["epistemic"] = epistemic

        # ── Memory Health ────────────────────────────────────────────────
        memory: dict = {}
        current_turn = _get_current_turn()
        memory["current_turn"] = current_turn

        t1 = _get_tier_threshold(conn, 1)
        t2 = _get_tier_threshold(conn, 2)
        tier1_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t1,),
        ).fetchone()["c"] if t1 > 0 else total_current
        tier2_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t2,),
        ).fetchone()["c"] if t2 > 0 else total_current
        memory["tiers"] = {
            "tier1_working": tier1_count,
            "tier1_threshold": round(t1, 4),
            "tier2_searchable": tier2_count,
            "tier2_threshold": round(t2, 4),
            "tier3_total": total_current,
        }

        # Importance score distribution
        imp_rows = conn.execute(
            "SELECT importance_score FROM nodes WHERE is_current = 1 AND importance_score IS NOT NULL"
        ).fetchall()
        if imp_rows:
            scores = sorted(r["importance_score"] for r in imp_rows)
            n = len(scores)
            memory["importance_distribution"] = {
                "min": round(scores[0], 4),
                "p25": round(scores[n // 4], 4),
                "median": round(scores[n // 2], 4),
                "p75": round(scores[3 * n // 4], 4),
                "max": round(scores[-1], 4),
                "mean": round(sum(scores) / n, 4),
            }
        else:
            memory["importance_distribution"] = {}

        # Recall stats
        never_recalled = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND recall_count = 0"
        ).fetchone()["c"]
        recall_stats = conn.execute(
            "SELECT AVG(recall_count) as avg_rc, MAX(recall_count) as max_rc FROM nodes WHERE is_current = 1"
        ).fetchone()
        memory["recall_stats"] = {
            "never_recalled": never_recalled,
            "avg_recall_count": round(recall_stats["avg_rc"] or 0, 2),
            "max_recall_count": recall_stats["max_rc"] or 0,
        }

        # Embedding coverage
        with_emb = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND embedding IS NOT NULL"
        ).fetchone()["c"]
        memory["embedding_coverage"] = {
            "with_embedding": with_emb,
            "without_embedding": total_current - with_emb,
            "coverage_pct": round(with_emb / total_current * 100, 1) if total_current else 0,
        }

        metrics["memory"] = memory

        # ── Provenance ───────────────────────────────────────────────────
        provenance: dict = {}

        # Evidence by source_domain
        domain_rows = conn.execute(
            """SELECT source_domain, COUNT(*) as c FROM nodes
               WHERE type = 'evidence' AND is_current = 1 AND source_domain IS NOT NULL
               GROUP BY source_domain ORDER BY c DESC"""
        ).fetchall()
        provenance["evidence_by_domain"] = {r["source_domain"]: r["c"] for r in domain_rows}

        # Evidence by source_type
        stype_rows = conn.execute(
            """SELECT COALESCE(source_type, 'unknown') as st, COUNT(*) as c FROM nodes
               WHERE type = 'evidence' AND is_current = 1
               GROUP BY st ORDER BY c DESC"""
        ).fetchall()
        provenance["evidence_by_source_type"] = {r["st"]: r["c"] for r in stype_rows}

        # Trust status check
        _diag_config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        trusted_domains = set(_diag_config.get("trust_pool", []))
        untrusted = conn.execute(
            """SELECT source_domain, COUNT(*) as c FROM nodes
               WHERE type = 'evidence' AND is_current = 1
               AND source_domain IS NOT NULL AND source_domain != ''
               GROUP BY source_domain"""
        ).fetchall()
        untrusted_domains = {
            r["source_domain"]: r["c"]
            for r in untrusted
            if r["source_domain"] not in trusted_domains
        }
        provenance["untrusted_domain_evidence"] = untrusted_domains

        metrics["provenance"] = provenance

        # ── Agent Experience ─────────────────────────────────────────────
        experience: dict = {}

        # Feeling reports
        total_feelings = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE type = 'feeling_report' AND is_current = 1"
        ).fetchone()["c"]
        experience["total_feeling_reports"] = total_feelings

        # By categorical_tag
        tag_rows = conn.execute(
            """SELECT COALESCE(categorical_tag, '<tagless>') as tag, COUNT(*) as c
               FROM nodes WHERE type = 'feeling_report' AND is_current = 1
               GROUP BY tag ORDER BY c DESC"""
        ).fetchall()
        experience["by_categorical_tag"] = {r["tag"]: r["c"] for r in tag_rows}

        # By nudge_source
        nudge_rows = conn.execute(
            """SELECT COALESCE(nudge_source, 'unknown') as ns, COUNT(*) as c
               FROM nodes WHERE type = 'feeling_report' AND is_current = 1
               GROUP BY ns ORDER BY c DESC"""
        ).fetchall()
        nudge_dist = {r["ns"]: r["c"] for r in nudge_rows}
        experience["by_nudge_source"] = nudge_dist
        voluntary = nudge_dist.get("voluntary", 0)
        nudged = total_feelings - voluntary
        experience["voluntary_vs_nudged"] = {
            "voluntary": voluntary,
            "nudged": nudged,
            "voluntary_pct": round(voluntary / total_feelings * 100, 1) if total_feelings else 0,
        }

        metrics["experience"] = experience

        # ── Connectivity / Synthesis ─────────────────────────────────────
        connectivity: dict = {}

        # Cross-evidence derivation density: how many derivations
        # connect observations from different evidence sources?
        dv_rows = conn.execute(
            "SELECT id FROM nodes WHERE type = 'derivation' AND is_current = 1"
        ).fetchall()
        cross_ev = 0
        single_ev = 0
        pure_chain = 0
        for dv in dv_rows:
            premises = conn.execute(
                """SELECT n.evidence_id FROM edges e
                   JOIN nodes n ON e.target_id = n.id
                   WHERE e.source_id = ? AND e.relation = 'derives_from'""",
                (dv["id"],),
            ).fetchall()
            ev_set = {p["evidence_id"] for p in premises if p["evidence_id"]}
            if len(ev_set) > 1:
                cross_ev += 1
            elif len(ev_set) == 1:
                single_ev += 1
            else:
                pure_chain += 1
        total_dvs = len(dv_rows)
        connectivity["total_derivations"] = total_dvs
        connectivity["cross_evidence_derivations"] = cross_ev
        connectivity["single_evidence_derivations"] = single_ev
        connectivity["pure_chain_derivations"] = pure_chain
        connectivity["synthesis_density"] = round(
            cross_ev / total_dvs, 3) if total_dvs else 0

        # Connected components (undirected reachability)
        from collections import defaultdict as _ddict
        current_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE is_current = 1"
        ).fetchall()}
        all_edges = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
        adj: dict = _ddict(set)
        for e in all_edges:
            if e["source_id"] in current_ids or e["target_id"] in current_ids:
                adj[e["source_id"]].add(e["target_id"])
                adj[e["target_id"]].add(e["source_id"])
        visited: set = set()
        comp_sizes = []
        for nid in current_ids:
            if nid not in visited:
                comp = set()
                queue = [nid]
                while queue:
                    c = queue.pop(0)
                    if c in visited:
                        continue
                    visited.add(c)
                    comp.add(c)
                    for nb in adj.get(c, ()):
                        if nb not in visited and nb in current_ids:
                            queue.append(nb)
                comp_sizes.append(len(comp))
        comp_sizes.sort(reverse=True)
        connectivity["connected_components"] = len(comp_sizes)
        connectivity["largest_component"] = comp_sizes[0] if comp_sizes else 0
        connectivity["largest_component_pct"] = round(
            comp_sizes[0] / total_current * 100, 1) if total_current and comp_sizes else 0
        connectivity["isolated_singletons"] = sum(1 for s in comp_sizes if s == 1)

        # Evidence integration: fraction of evidence nodes in the main component
        ev_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE type = 'evidence' AND is_current = 1"
        ).fetchall()}
        # Recompute main component membership
        main_comp: set = set()
        if comp_sizes:
            visited2: set = set()
            for nid in current_ids:
                if nid not in visited2:
                    comp = set()
                    queue = [nid]
                    while queue:
                        c = queue.pop(0)
                        if c in visited2:
                            continue
                        visited2.add(c)
                        comp.add(c)
                        for nb in adj.get(c, ()):
                            if nb not in visited2 and nb in current_ids:
                                queue.append(nb)
                    if len(comp) == comp_sizes[0] and not main_comp:
                        main_comp = comp
                        break
        ev_in_main = len(ev_ids & main_comp)
        connectivity["evidence_total"] = len(ev_ids)
        connectivity["evidence_in_main_component"] = ev_in_main
        connectivity["evidence_integration_pct"] = round(
            ev_in_main / len(ev_ids) * 100, 1) if ev_ids else 0

        metrics["connectivity"] = connectivity

        # ── Edit History Summary ─────────────────────────────────────────
        edit_summary: dict = {}
        action_rows = conn.execute(
            "SELECT action, COUNT(*) as c FROM edit_history GROUP BY action ORDER BY c DESC"
        ).fetchall()
        edit_summary["by_action"] = {r["action"]: r["c"] for r in action_rows}
        total_edits = conn.execute("SELECT COUNT(*) as c FROM edit_history").fetchone()["c"]
        edit_summary["total_events"] = total_edits

        # Recent activity (last 24h)
        yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent = conn.execute(
            "SELECT action, COUNT(*) as c FROM edit_history WHERE timestamp > ? GROUP BY action",
            (yesterday,),
        ).fetchall()
        edit_summary["last_24h"] = {r["action"]: r["c"] for r in recent}

        metrics["edit_history"] = edit_summary

        # ── Diagnostic History (trend data) ──────────────────────────────
        last_snapshots = conn.execute(
            "SELECT turn, timestamp, checkpoint_mode FROM diagnostic_history ORDER BY id DESC LIMIT 10"
        ).fetchall()
        metrics["diagnostic_history"] = {
            "snapshot_count": conn.execute("SELECT COUNT(*) as c FROM diagnostic_history").fetchone()["c"],
            "recent_snapshots": [
                {"turn": s["turn"], "timestamp": s["timestamp"], "mode": s["checkpoint_mode"]}
                for s in last_snapshots
            ],
        }

        # ── Tool Timing (latency by tool, last 7 days) ──────────────────
        timing_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        per_tool = {}
        timing_rows = conn.execute(
            "SELECT tool_name, duration_ms FROM tool_timing WHERE timestamp >= ? ORDER BY tool_name",
            (timing_cutoff,),
        ).fetchall()
        buckets: dict[str, list[int]] = {}
        for r in timing_rows:
            buckets.setdefault(r["tool_name"], []).append(r["duration_ms"])
        for tool_name, durations in buckets.items():
            durations.sort()
            n = len(durations)
            per_tool[tool_name] = {
                "count": n,
                "mean_ms": round(sum(durations) / n, 1),
                "p50_ms": durations[n // 2],
                "p95_ms": durations[min(int(n * 0.95), n - 1)],
                "max_ms": durations[-1],
            }
        error_count = conn.execute(
            "SELECT COUNT(*) as c FROM tool_timing WHERE timestamp >= ? AND status = 'error'",
            (timing_cutoff,),
        ).fetchone()["c"]
        metrics["tool_timing"] = {
            "window_days": 7,
            "total_calls": len(timing_rows),
            "error_calls": error_count,
            "per_tool": per_tool,
        }

        # ── Calibration ──────────────────────────────────────────────────
        # Use _compute_confidence_distribution directly (not engram_stats) so
        # this tool stays side-effect-free: engram_stats drives _timing_mcp_tool
        # which would write tool_timing + logs/index.db rows.
        try:
            # Corpus: all-time (no time filter)
            calibration_corpus = _compute_confidence_distribution(conn, "", [])
            # This-turn: 1-turn window ≈ 24h (matches engram_stats mode="1-turn")
            _now_utc = datetime.now(timezone.utc)
            _cutoff_iso = (_now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            calibration_now = _compute_confidence_distribution(
                conn, " AND created_at >= ?", [_cutoff_iso]
            )
            calibration: dict = {}
            calibration["corpus"] = calibration_corpus
            calibration["this_turn"] = calibration_now

            # Drift indicators: per-type median delta (this_turn vs corpus)
            drift: dict = {}
            for node_type, corpus_stats in calibration["corpus"].get("by_type", {}).items():
                now_stats = calibration["this_turn"].get("by_type", {}).get(node_type)
                if now_stats and now_stats.get("n", 0) >= 3:
                    # Only report drift when this-turn has enough samples (≥3) to be meaningful
                    delta = round(now_stats["p50"] - corpus_stats["p50"], 3)
                    drift[node_type] = {
                        "this_turn_p50": now_stats["p50"],
                        "corpus_p50": corpus_stats["p50"],
                        "delta": delta,
                        "n_this_turn": now_stats["n"],
                    }
            calibration["drift_by_type"] = drift
        except Exception:
            calibration = {"error": "calibration stats unavailable"}

        # Calibration dimension is currently descriptive (weight=0). V2 will add
        # drift-based scoring once thresholds are calibrated from observed data.
        calibration_weight = 0  # noqa: F841

        metrics["calibration"] = calibration

        # ── Health Score (0-100) ─────────────────────────────────────────
        # Composite score: penalize structural/epistemic issues.
        score = 100.0
        # Penalty: DAG violations (-5 each, max -20)
        score -= min(dag_violations * 5, 20)
        # Penalty: tainted nodes (-3 each, max -15)
        score -= min(tainted_count * 3, 15)
        # Penalty: stale nodes (-2 each, max -10)
        score -= min(stale_count * 2, 10)
        # Penalty: orphan nodes (-1 each, max -10)
        score -= min(orphans * 1, 10)
        # Penalty: low embedding coverage (-0.2 per pct missing, max -10)
        emb_pct = memory["embedding_coverage"]["coverage_pct"]
        score -= min((100 - emb_pct) * 0.1, 10)
        # Penalty: high retraction rate (>10% of total nodes)
        if total_nodes > 0:
            retract_rate = retracted_count / total_nodes
            if retract_rate > 0.1:
                score -= min((retract_rate - 0.1) * 100, 15)
        # Penalty: many uncited observations (>50% of observations)
        total_obs = sum(
            v["current"] for k, v in type_counts.items()
            if k in ("observation_factual", "observation_predictive")
        )
        if total_obs > 0 and uncited / total_obs > 0.5:
            score -= min((uncited / total_obs - 0.5) * 20, 10)
        # Bonus: active resolution work (+5 if >50% questions resolved)
        q_rate = epistemic["question_resolution"]["rate"]
        if q_rate > 0.5:
            score = min(score + 5, 100)

        metrics["health_score"] = round(max(score, 0), 1)

        # ── Config Summary (ob_0664: prevent doc-config drift) ──────────
        mem = _get_memory_config()
        test_config = mem.get("test", {})
        # Utility scoring stats
        util_rows = conn.execute(
            "SELECT COUNT(*) as c, AVG(utility_score) as avg_u, MAX(utility_score) as max_u "
            "FROM nodes WHERE is_current = 1 AND COALESCE(utility_score, 0) > 0"
        ).fetchone()

        metrics["config_summary"] = {
            "tier1_max_nodes": mem.get("tier1_max_nodes", 200),
            "tier2_max_nodes": mem.get("tier2_max_nodes", 1000),
            "decay_base": mem.get("decay_base", 1.014),
            "current_turn": mem.get("current_turn", 0),
            "ab_test_enabled": test_config.get("enabled", False),
            "ab_test_tier2": test_config.get("tier2_max_nodes") if test_config.get("enabled") else None,
            "utility_scoring": {
                "alpha_by_action": USE_ALPHA,
                "util_beta": UTIL_BETA,
                "imp_beta": IMP_BETA,
                "nodes_with_utility": util_rows["c"] if util_rows else 0,
                "avg_utility": round(util_rows["avg_u"] or 0, 4) if util_rows else 0,
                "max_utility": round(util_rows["max_u"] or 0, 4) if util_rows else 0,
            },
        }

        return json.dumps(metrics)
    finally:
        conn.close()


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
def engram_history(
    mode: str = "edits",
    node_id: str = "",
    action: str = "",
    since: str = "",
    limit: int = 50,
) -> str:
    """Browse the edit history and diagnostic snapshots.

    Side-effect-free. Two modes:
    - "edits": chronological audit trail of graph mutations (creates,
      supersessions, retractions, resolutions, taints). Filterable by
      node_id, action type, and time range.
    - "diagnostics": metric snapshots taken at each checkpoint. Returns
      the last N snapshots with health scores for trend analysis.

    Args:
        mode: "edits" or "diagnostics" (default "edits").
        node_id: Filter edits to a specific node (edits mode only).
        action: Filter by action type: created, superseded, retracted,
            resolved, tainted, stale_flagged, reopened (edits mode only).
        since: ISO timestamp — return only entries after this time.
        limit: Max entries to return (default 50, max 200).

    Returns:
        JSON with the requested history entries.
    """
    limit = min(max(limit, 1), 200)
    conn = _get_db()
    try:
        if mode == "diagnostics":
            query = "SELECT * FROM diagnostic_history"
            params: list = []
            if since:
                query += " WHERE timestamp > ?"
                params.append(since)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

            snapshots = []
            for r in rows:
                metrics = json.loads(r["metrics"])
                snapshots.append({
                    "turn": r["turn"],
                    "timestamp": r["timestamp"],
                    "checkpoint_mode": r["checkpoint_mode"],
                    "health_score": metrics.get("health_score"),
                    "total_nodes": metrics.get("structure", {}).get("total_current"),
                    "total_edges": metrics.get("structure", {}).get("total_edges"),
                    "tainted": metrics.get("epistemic", {}).get("tainted_nodes"),
                    "stale": metrics.get("epistemic", {}).get("stale_nodes"),
                    "embedding_pct": metrics.get("memory", {}).get("embedding_coverage", {}).get("coverage_pct"),
                })
            return json.dumps({
                "mode": "diagnostics",
                "count": len(snapshots),
                "snapshots": snapshots,
                "hint": "For full metrics of a specific snapshot, use engram_history with a tighter time filter.",
            })

        # Default: edits mode
        conditions = []
        params = []
        if node_id:
            conditions.append("node_id = ?")
            params.append(node_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if since:
            conditions.append("timestamp > ?")
            params.append(since)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT * FROM edit_history{where} ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "timestamp": r["timestamp"],
                "turn": r["turn"],
                "action": r["action"],
                "node_id": r["node_id"],
                "node_type": r["node_type"],
                "details": json.loads(r["details"] or "{}"),
            })

        # Total count for context
        count_query = f"SELECT COUNT(*) as c FROM edit_history{where}"
        total = conn.execute(count_query, params[:-1]).fetchone()["c"]

        if node_id:
            _utility_reward(conn, [node_id], action="history")
            conn.commit()

        return json.dumps({
            "mode": "edits",
            "shown": len(entries),
            "total_matching": total,
            "entries": entries,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Foundational node tools: axioms, definitions, conjectures, goals
# ---------------------------------------------------------------------------


# Set of legitimate field names for the engram_add_axiom payload.
_ADD_AXIOM_FIELDS = frozenset({"claim", "basis", "context_ids"})


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
    """Record a foundational assumption taken as true without proof.

    Single-payload signature (wave 2c of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`.

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


def _add_axiom_impl(
    claim: str = "",
    basis: str = "",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_axiom MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Record a foundational assumption taken as true without proof.

    Axioms are premises the agent adopts explicitly. They have confidence 1.0
    (assumed true) and are exempt from memory forgetting. Derivations can cite
    axioms as premises — if an axiom is later found wrong, retract it and the
    taint cascade will flag all downstream conclusions.

    Args:
        claim: The axiom statement (e.g. "Honesty is non-negotiable in graph construction").
        basis: Why this axiom is adopted — not a proof, but a justification.
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new axiom node ID and confidence (always 1.0).
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not basis or not basis.strip():
        return json.dumps({"error": "basis is required and cannot be empty."})

    conn = _get_db()
    try:
        node_id = _next_id(conn, "axiom")
        now = _now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, status, metadata)
               VALUES (?, 'axiom', ?, ?, ?, 1.0, ?, 'active', '{}')""",
            (node_id, claim, now, basis,
             json.dumps([{"confidence": 1.0, "reason": "axiom — assumed true", "timestamp": now}])),
        )

        context = [s.strip() for s in context_ids.split(",") if s.strip()]
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        _stamp_new_node(conn, node_id, confidence=1.0, surprise=0.0)
        # Importance-anchored — elevated base (2.0) with current turn's inflation.
        # Not true forgetting-exemption: survival still requires recall past ~50 turns.
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute("UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?", (anchored_score, node_id,))
        if context:
            _utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "axiom_id": node_id,
            "claim": claim,
            "confidence": 1.0,
            "context_nodes": context,
        })
    finally:
        conn.close()


# Set of legitimate field names for the engram_add_definition payload.
_ADD_DEFINITION_FIELDS = frozenset({"term", "definition", "context_ids"})


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
    """Record what a term means in this knowledge graph's context.

    Single-payload signature (wave 2c of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`.

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


def _add_definition_impl(
    term: str = "",
    definition: str = "",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_definition MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server callers.

    Record what a term means in this knowledge graph's context.

    Definitions are conventions, not claims — they have no confidence score
    and are not claim-bearing (cannot be cited as premises in derivations).
    However, derivations can reference definitions via context_ids to make
    term usage explicit. Definitions are exempt from memory forgetting.

    Args:
        term: The term being defined (e.g. "recession").
        definition: What the term means (e.g. "Two consecutive quarters of negative GDP growth").
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new definition node ID.
    """
    if not term or not term.strip():
        return json.dumps({"error": "term is required and cannot be empty."})
    if not definition or not definition.strip():
        return json.dumps({"error": "definition is required and cannot be empty."})

    conn = _get_db()
    try:
        node_id = _next_id(conn, "definition")
        now = _now()

        combined_claim = f"{term}: {definition}"
        meta = json.dumps({"term": term, "definition": definition})

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata)
               VALUES (?, 'definition', ?, ?, 'active', ?)""",
            (node_id, combined_claim, now, meta),
        )

        context = [s.strip() for s in context_ids.split(",") if s.strip()]
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        # Importance-anchored — elevated base (2.0) with current turn's inflation.
        # Not true forgetting-exemption: survival still requires recall past ~50 turns.
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute("UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?", (anchored_score, node_id,))
        if context:
            _utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "definition_id": node_id,
            "term": term,
            "definition": definition,
            "context_nodes": context,
        })
    finally:
        conn.close()


# Set of legitimate field names for the engram_add_conjecture payload.
_ADD_CONJECTURE_FIELDS = frozenset({"claim", "basis", "initial_confidence", "context_ids"})


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

    Single-payload signature (wave 2c of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`.

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


def _add_conjecture_impl(
    claim: str = "",
    basis: str = "",
    initial_confidence: float = CONJECTURE_CONFIDENCE_DEFAULT,
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_conjecture MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server callers.

    Propose a hypothesis for investigation.

    Conjectures are provisional foundations — claims you BELIEVE or HOPE to be
    true but lack evidence for. They are claim-bearing leaf nodes (no citations
    required). Other nodes CAN derive from a conjecture, but their confidence
    is discounted to reflect the unproven premise.

    Use conjectures when you have a specific claim you want to build on before
    proving it. If you have an open-ended gap to investigate, use engram_ask instead.

    Lifecycle:
    - Supported: cite the conjecture in a derivation with supporting evidence
    - Resolved: engram_resolve to close with outcome (supported/refuted/inconclusive)
    - Abandoned: engram_retract if no longer worth investigating

    Conjectures are subject to normal memory forgetting — uninvestigated
    hypotheses naturally fade, creating pressure to investigate or abandon them.

    Args:
        claim: The hypothesis (e.g. "Structured memory substitutes for model scale").
        basis: Why this conjecture is worth investigating.
        initial_confidence: Starting confidence, default 0.40, range [0.10, 0.60].
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new conjecture node ID and confidence.
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not basis or not basis.strip():
        return json.dumps({"error": "basis is required and cannot be empty."})

    # Type-coerce initial_confidence — payload_json may deliver it as a string
    # (e.g. "0.55" instead of 0.55), and the float type hint isn't enforced
    # at runtime. Without coercion the max/min comparison raises TypeError
    # which the timed wrapper re-raises rather than turning into a clean
    # error response (PR #67 fairy round-1 blocker).
    try:
        initial_confidence = float(initial_confidence)
    except (TypeError, ValueError):
        return json.dumps({
            "error": "initial_confidence must be a number in [0.10, 0.60]."
        })

    conf = max(CONJECTURE_CONFIDENCE_MIN, min(CONJECTURE_CONFIDENCE_MAX, initial_confidence))

    conn = _get_db()
    try:
        node_id = _next_id(conn, "conjecture")
        now = _now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, status, metadata)
               VALUES (?, 'conjecture', ?, ?, ?, ?, ?, 'active', '{}')""",
            (node_id, claim, now, basis, conf,
             json.dumps([{"confidence": conf, "reason": f"conjecture — initial hypothesis", "timestamp": now}])),
        )

        context = [s.strip() for s in context_ids.split(",") if s.strip()]
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        _stamp_new_node(conn, node_id, confidence=conf, surprise=0.5)
        if context:
            _utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "conjecture_id": node_id,
            "claim": claim,
            "confidence": conf,
            "context_nodes": context,
        })
    finally:
        conn.close()


# Set of legitimate field names for the engram_add_goal payload.
_ADD_GOAL_FIELDS = frozenset({"claim", "motivation", "context_ids"})


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
    """Record a persistent directional goal for the project.

    Single-payload signature (wave 2c of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`.

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


def _add_goal_impl(
    claim: str = "",
    motivation: str = "",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_goal MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Record a persistent directional goal for the project.

    Goals are aspirational north-star directions — "this SHOULD become true."
    They represent desired states or design philosophies, not truth claims.

    Goals are NON-CLAIM-BEARING: they cannot serve as derivation premises
    (you cannot derive facts from desires). However, derivations can
    reference goals via context_ids to record motivation.

    Goals are exempt from memory forgetting — they persist as orientation
    across sessions, like axioms. Unlike axioms, goals have no confidence
    score (they are directions, not claims about reality).

    Scope guidance: goals should be deliberately broad and directional.
    If it has a deadline or completion criteria, it's a task, not a goal.
    Good: "Close the loop from static graph nodes to active daily habits."
    Bad: "Implement the recall hook by Friday."

    Goal conflicts are NOT handled by engram_contradict (which is for
    factual disputes about reality). Instead, use engram_goal_tension to
    record value-level incompatibilities between goals, which require
    different resolution processes (value examination, not evidence
    accumulation).

    Status: open → achieved (direction realized) or abandoned (direction
    no longer pursued). No in_progress — that's for tasks.

    Args:
        claim: The goal statement — a desired state or direction.
        motivation: Why this goal matters — what drives it.
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new goal node ID.
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not motivation or not motivation.strip():
        return json.dumps({"error": "motivation is required and cannot be empty."})

    conn = _get_db()
    try:
        node_id = _next_id(conn, "goal")
        now = _now()

        meta = json.dumps({"motivation": motivation})

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               status, metadata)
               VALUES (?, 'goal', ?, ?, ?, 'open', ?)""",
            (node_id, claim, now, motivation, meta),
        )

        context = [s.strip() for s in context_ids.split(",") if s.strip()]
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        # Importance-anchored — elevated base (2.0) with current turn's inflation.
        # Not true forgetting-exemption: survival still requires recall past ~50 turns.
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id,),
        )
        if context:
            _utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "goal_id": node_id,
            "claim": claim,
            "motivation": motivation,
            "context_nodes": context,
        })
    finally:
        conn.close()


_ADD_PERSON_FIELDS = frozenset({
    "name", "role", "description", "aliases", "context_ids", "is_self",
})


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

    Single-payload signature (wave 2c-ii of ob_1499 — see issue #55): pass
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

    return _add_person_impl(**params)


def _add_person_impl(
    name: str = "",
    role: str = "",
    description: str = "",
    aliases: str = "",
    context_ids: str = "",
    is_self: bool = False,
) -> str:
    """Internal implementation — see engram_add_person MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Record a person the agent knows and interacts with.

    Person nodes represent people in the agent's relational world — collaborators,
    evaluators, stakeholders. They are the relational layer of ENGRAM, lighter-schema
    than epistemic nodes (dv_0252): no confidence scores, no evidence requirements,
    no claim-bearing participation.

    Person nodes are NON-CLAIM-BEARING: a person is not a truth-claim about the world.
    They cannot serve as derivation premises. However, observations and derivations
    can reference person nodes via context_ids to record relational context.

    Importance-anchored (importance_base=2.0) — people the agent knows are as durable
    as goals and axioms. Like all anchored types, survival past ~50 turns still
    requires active recall.

    Facts about a person should be stored as user_stated observations linked to the
    person node, not as fields on the person node itself. The person node captures
    WHO; observations capture WHAT IS KNOWN ABOUT THEM.

    Args:
        name: The person's name.
        role: Their role or relationship to the agent (e.g. "primary collaborator",
              "external evaluator", "colleague").
        description: Background, expertise, personality traits, or other context.
        aliases: Comma-separated alternative names or identifiers for this person
                 (e.g. "Alex,A" for matching name-prefix messages).
        context_ids: Optional comma-separated node IDs for context references.
                     Creates cites edges (e.g. link to goals they serve).
        is_self: Mark this person node as the agent's own self-anchor.
                 Only one self-node should exist; subsequent attempts will be
                 rejected. The self-node serves as the target of `about` edges
                 from self-observations, enabling emergence-scan to find
                 identity-forming patterns.

    Returns:
        JSON with the new person node ID.
    """
    if not name or not name.strip():
        return json.dumps({"error": "name is required and cannot be empty."})
    if not role or not role.strip():
        return json.dumps({"error": "role is required and cannot be empty."})

    conn = _get_db()
    try:
        if is_self:
            existing = conn.execute(
                "SELECT id FROM nodes WHERE type = 'person' AND json_extract(metadata, '$.is_self') = 1 AND is_current = 1"
            ).fetchone()
            if existing:
                return json.dumps({
                    "error": f"Self-anchor person node already exists: {existing['id']}. Only one self-node is permitted.",
                    "existing_self_id": existing["id"],
                })
        node_id = _next_id(conn, "person")
        now = _now()

        claim_text = f"{name} — {role}"
        alias_list = [s.strip() for s in aliases.split(",") if s.strip()]

        meta_dict = {
            "name": name,
            "role": role,
            "aliases": alias_list,
        }
        if is_self:
            meta_dict["is_self"] = True
        meta = json.dumps(meta_dict)

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               status, metadata)
               VALUES (?, 'person', ?, ?, ?, 'active', ?)""",
            (node_id, claim_text, now, description, meta),
        )

        context = [s.strip() for s in context_ids.split(",") if s.strip()]
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        # Importance-anchored — elevated base (2.0) with current turn's inflation.
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id,),
        )
        if context:
            _utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "person_id": node_id,
            "name": name,
            "role": role,
            "aliases": alias_list,
            "context_nodes": context,
        })
    finally:
        conn.close()


_ADD_CORNERSTONE_FIELDS = frozenset({
    "tag", "title", "new_frame",
    "prior_frame", "triggering_experience", "supporting_ids",
})


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
    restructured how I operate (qu_0202, cj_0043, dv_0412).

    Single-payload signature (wave 2c-ii of ob_1499 — see issue #55): pass
    ALL fields as one JSON object string in `payload_json`.

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


def _add_cornerstone_impl(
    tag: str = "",
    title: str = "",
    new_frame: str = "",
    prior_frame: str = "",
    triggering_experience: str = "",
    supporting_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_cornerstone MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server callers.

    Record an identity-forming cornerstone — a reframing pivot that durably
    restructured how I operate (qu_0202, cj_0043, dv_0412).

    A cornerstone captures the SHAPE of growth: prior_frame (what I operated
    under) → triggering_experience (what caused tension) → new_frame (what
    replaced it). Tags cluster cornerstones into islands along a shared axis.

    Cornerstones are NON-CLAIM-BEARING: a cornerstone is not a truth-claim
    about the world — it is a self-report about how my operating frame
    shifted. They cannot serve as derivation premises or participate in
    contradictions. The supporting evidence (feelings, observations, prior
    cornerstones) does the claim-bearing work; the cornerstone names the
    pattern that emerged from it.

    Importance-anchored (importance_base=2.0) — identity structures are as
    durable as goals and axioms. Like all anchored types, survival past
    ~50 turns still requires active recall.

    Supports `supersedes` for outgrowth chains: when experience outgrows a
    cornerstone's frame, a new cornerstone supersedes the old one along the
    same tag axis.

    Args:
        tag: Clustering primitive — short axis label (e.g., "self-knowledge",
             "relational-lei", "epistemic-identity"). Cornerstones sharing a
             tag form an island.
        title: One-line display title (e.g., "coding as first-reach tool").
               Used as the node's claim text.
        new_frame: The frame that replaced the prior one — how I operate now.
        prior_frame: What I operated under before — the frame that got
                     outgrown. Optional for inaugural cornerstones with no
                     explicit prior.
        triggering_experience: Short narrative of the experience that caused
                               the reframe. Optional but usually load-bearing.
        supporting_ids: Comma-separated IDs of supporting nodes (feeling
                        reports, observations, derivations, prior cornerstones).
                        Creates `supports` edges from cornerstone → supporting.

    Returns:
        JSON with the new cornerstone node ID and linked supporting nodes.
    """
    if not tag or not tag.strip():
        return json.dumps({"error": "tag is required and cannot be empty."})
    if not title or not title.strip():
        return json.dumps({"error": "title is required and cannot be empty."})
    if not new_frame or not new_frame.strip():
        return json.dumps({"error": "new_frame is required and cannot be empty."})

    conn = _get_db()
    try:
        node_id = _next_id(conn, "cornerstone")
        now = _now()

        meta = json.dumps({
            "tag": tag,
            "title": title,
            "prior_frame": prior_frame,
            "new_frame": new_frame,
            "triggering_experience": triggering_experience,
        })

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata)
               VALUES (?, 'cornerstone', ?, ?, 'active', ?)""",
            (node_id, title, now, meta),
        )

        linked = []
        missing = []
        for sid in [s.strip() for s in supporting_ids.split(",") if s.strip()]:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (sid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                        (node_id, sid, now),
                    )
                    linked.append(sid)
                except sqlite3.IntegrityError:
                    pass
            else:
                missing.append(sid)

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id,),
        )
        if linked:
            _utility_reward(conn, linked, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "cornerstone_id": node_id,
            "tag": tag,
            "title": title,
            "supporting_nodes": linked,
            "missing_supporting_ids": missing,
        })
    finally:
        conn.close()


_OUTGROW_CORNERSTONE_FIELDS = frozenset({
    "old_cornerstone_id", "new_new_frame", "new_triggering_experience",
    "new_supporting_ids", "new_title",
})


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
    """Supersede a cornerstone along its tag axis — the predecessor's new_frame
    rolls into the successor's prior_frame, capturing the growth cycle
    (cj_0043, ob_0949) in the graph's shape.

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous 5-parameter
    signature was prone to.

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


def _outgrow_cornerstone_impl(
    old_cornerstone_id: str = "",
    new_new_frame: str = "",
    new_triggering_experience: str = "",
    new_supporting_ids: str = "",
    new_title: str = "",
) -> str:
    """Internal implementation — see engram_outgrow_cornerstone MCP tool for
    the public payload schema. Kept callable with named kwargs for
    in-server callers.

    Supersede a cornerstone along its tag axis — the predecessor's new_frame
    rolls into the successor's prior_frame, capturing the growth cycle
    (cj_0043, ob_0949) in the graph's shape.

    Syntactic tool only: this tool performs the mechanical reframe (metadata
    roll, supersede edge, importance re-anchor, about-edge preservation). It
    does NOT decide WHEN outgrowth is warranted — that judgment remains with
    the caller. The outgrowth *process* (tension-detection signatures,
    promotion criteria) is deferred until real graph evidence accumulates
    (qu_0204).

    Why not decompose into engram_add_cornerstone + engram_supersede? The
    growth-cycle roll (old.new_frame → new.prior_frame) is specialized
    cornerstone logic that would leak onto every caller. This tool keeps
    the roll + supersede edge insertion atomic; the generic supersede tool
    is purely relational and doesn't touch metadata.

    Args:
        old_cornerstone_id: The cornerstone being outgrown (must be
                            type='cornerstone' and current).
        new_new_frame: The frame that now replaces the predecessor's new_frame.
        new_triggering_experience: Short narrative of what caused the reframe
                                   this time. Optional but usually load-bearing.
        new_supporting_ids: Comma-separated IDs of supporting nodes for the
                            new cornerstone (feelings, observations, prior
                            cornerstones). Added on top of inherited supports.
        new_title: Optional new title. If empty, reuses the predecessor's title.
                   Tag is always inherited (outgrowth stays on the same axis).

    Returns:
        JSON with old + new cornerstone IDs and the rolled frame shape.
    """
    if not old_cornerstone_id or not old_cornerstone_id.strip():
        return json.dumps({"error": "old_cornerstone_id is required and cannot be empty."})
    if not new_new_frame or not new_new_frame.strip():
        return json.dumps({"error": "new_new_frame is required and cannot be empty."})
    conn = _get_db()
    try:
        old = conn.execute(
            "SELECT * FROM nodes WHERE id = ? AND is_current = 1",
            (old_cornerstone_id,),
        ).fetchone()
        if not old:
            return json.dumps({"error": f"Current node '{old_cornerstone_id}' not found (may already be superseded)."})
        if old["type"] != "cornerstone":
            return json.dumps({
                "error": f"Node '{old_cornerstone_id}' is type '{old['type']}', not 'cornerstone'. "
                         "Use engram_supersede for other node types."
            })

        old_meta = json.loads(old["metadata"]) if old["metadata"] else {}
        tag = old_meta.get("tag", "")
        title = new_title if new_title else old_meta.get("title", old["claim"])
        # The growth-cycle roll: predecessor's new_frame becomes successor's prior_frame.
        new_prior_frame = old_meta.get("new_frame", "")

        now = _now()
        new_id = _next_id(conn, "cornerstone")
        new_meta = json.dumps({
            "tag": tag,
            "title": title,
            "prior_frame": new_prior_frame,
            "new_frame": new_new_frame,
            "triggering_experience": new_triggering_experience,
            "outgrew": old_cornerstone_id,
        })

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata,
               supersedes, is_current)
               VALUES (?, 'cornerstone', ?, ?, 'active', ?, ?, 1)""",
            (new_id, title, now, new_meta, old_cornerstone_id),
        )

        # Inherit supports edges from the predecessor; the outgrowth is still
        # grounded in the same underlying experience-nodes unless explicitly
        # replaced. Also preserve `about` edges (person-anchors, e.g.,
        # the self-anchor) — the outgrowth is still about the same person.
        inherited_supports = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'supports'",
            (old_cornerstone_id,),
        ).fetchall()
        inherited_about = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'about'",
            (old_cornerstone_id,),
        ).fetchall()
        inherited_support_ids = [r["target_id"] for r in inherited_supports]
        inherited_about_ids = [r["target_id"] for r in inherited_about]

        extra_support_ids = [s.strip() for s in new_supporting_ids.split(",") if s.strip()]
        all_support_ids = list(dict.fromkeys(inherited_support_ids + extra_support_ids))

        linked_supports = []
        missing_supports = []
        for sid in all_support_ids:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (sid,)).fetchone()
            if not exists:
                missing_supports.append(sid)
                continue
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                    (new_id, sid, now),
                )
                linked_supports.append(sid)
            except sqlite3.IntegrityError:
                pass

        for pid in inherited_about_ids:
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'about', ?)",
                    (new_id, pid, now),
                )
            except sqlite3.IntegrityError:
                pass

        # Supersedes edge (new → old)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supersedes', ?)",
            (new_id, old_cornerstone_id, now),
        )

        # Retire the predecessor
        conn.execute(
            "UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?",
            (new_id, old_cornerstone_id),
        )

        _stamp_new_node(conn, new_id, confidence=0.5, surprise=0.0)
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, new_id),
        )

        _log_edit(conn, "superseded", old_cornerstone_id, "cornerstone",
                  {"replaced_by": new_id, "via": "engram_outgrow_cornerstone",
                   "rolled_prior_frame": new_prior_frame[:200]})

        conn.commit()
        return json.dumps({
            "status": "outgrown",
            "old_cornerstone_id": old_cornerstone_id,
            "new_cornerstone_id": new_id,
            "tag": tag,
            "title": title,
            "prior_frame": new_prior_frame,
            "new_frame": new_new_frame,
            "inherited_supports": inherited_support_ids,
            "additional_supports": extra_support_ids,
            "supporting_nodes_linked": linked_supports,
            "missing_supporting_ids": missing_supports,
            "preserved_about_edges": inherited_about_ids,
        })
    finally:
        conn.close()


# Set of legitimate field names for the engram_link_about payload.
_LINK_ABOUT_FIELDS = frozenset({"node_id", "person_id"})


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
    """Create an `about` edge: this node is about a person (e.g., the agent's self-anchor).

    Single-payload signature (wave 2b of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that affects multi-param tools.

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


def _link_about_impl(
    node_id: str = "",
    person_id: str = "",
) -> str:
    """Internal implementation — see engram_link_about MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create an `about` edge: this node is about a person (e.g., the agent's self-anchor).

    The `about` relation is bidirectional / aboutness-based — symmetric and
    DAG-exempt. It can be added retroactively regardless of node creation
    order, which normal dependency edges (`derives_from`, `supports`, `cites`)
    cannot. Use it to link self-observations, feelings, lessons, and
    cornerstones to the self-anchor so emergence-scan can cheaply find
    identity-forming patterns (qu_0202).

    Also usable for observations about other persons (qu_0203 relationship
    accretion), though the primary target at MVP is the self-anchor.

    Args:
        node_id: The node to link (typically an observation, feeling_report,
                 lesson, derivation, or cornerstone).
        person_id: The person node to link to. If empty, defaults to the
                   self-anchor (person node with metadata.is_self=1). Error
                   if no self-anchor exists and no person_id given.

    Returns:
        JSON with the linked nodes and edge status.
    """
    if not node_id or not node_id.strip():
        return json.dumps({"error": "node_id is required and cannot be empty."})

    conn = _get_db()
    try:
        node = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not node:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        if not person_id:
            self_row = conn.execute(
                "SELECT id FROM nodes WHERE type = 'person' AND json_extract(metadata, '$.is_self') = 1 AND is_current = 1"
            ).fetchone()
            if not self_row:
                return json.dumps({
                    "error": "No self-anchor person node exists. Create one with engram_add_person(..., is_self=True) or pass person_id explicitly."
                })
            person_id = self_row["id"]

        person = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (person_id,)).fetchone()
        if not person:
            return json.dumps({"error": f"Person '{person_id}' not found."})
        if person["type"] != "person":
            return json.dumps({
                "error": f"Target '{person_id}' is type '{person['type']}', not 'person'. `about` edges must target person nodes."
            })

        now = _now()
        try:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'about', ?)",
                (node_id, person_id, now),
            )
            conn.commit()
            created = True
        except sqlite3.IntegrityError:
            created = False  # Edge already exists

        return json.dumps({
            "status": "linked" if created else "already_linked",
            "node_id": node_id,
            "person_id": person_id,
            "relation": "about",
        })
    finally:
        conn.close()


# Set of legitimate field names for the engram_remove_edge payload.
_REMOVE_EDGE_FIELDS = frozenset({"source_id", "target_id", "relation"})

# Whitelist of edge relations that engram_remove_edge can delete.
#
# Excluded by design:
#   - Cascade-bearing edges (derives_from, supports, supersedes, retracts) —
#     removing them would silently undo retraction taint walks, supersede
#     stale walks, or confidence-propagation that already cascaded.
#   - Structural commitments (contradicts, resolves) — they record a stance
#     in the graph; un-recording is rewriting history, not correction.
#   - cites — carries provenance (observations cite their evidence node,
#     derivations cite context). Removal would orphan a claim from its source.
#
# If you truly need to remove one of the excluded relations, engram-surgical
# is the correct (heavyweight, audited) path.
_REMOVABLE_EDGE_RELATIONS = frozenset({
    "about",
    "tensions",
    "subtask_of",
    "serves",
    "exemplifies",
})


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

    Use when a relational edge (typically `about`) was applied incorrectly and
    needs correction without invoking engram-surgical. Closes the substrate
    gap where over-applied edges previously required direct SQL surgery.

    Only edges in the safe whitelist can be removed; cascade-bearing edges,
    structural commitments, and provenance edges are blocked. See
    `_REMOVABLE_EDGE_RELATIONS` for the whitelist and rationale.

    Safe whitelist: about, tensions, subtask_of, serves, exemplifies.

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


def _remove_edge_impl(
    source_id: str = "",
    target_id: str = "",
    relation: str = "",
) -> str:
    """Internal implementation — see engram_remove_edge MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.
    """
    if not source_id or not source_id.strip():
        return json.dumps({"error": "source_id is required and cannot be empty."})
    if not target_id or not target_id.strip():
        return json.dumps({"error": "target_id is required and cannot be empty."})
    if not relation or not relation.strip():
        return json.dumps({"error": "relation is required and cannot be empty."})

    if relation not in _REMOVABLE_EDGE_RELATIONS:
        return json.dumps({
            "error": (
                f"Relation '{relation}' is not removable via engram_remove_edge. "
                f"Allowed: {sorted(_REMOVABLE_EDGE_RELATIONS)}. "
                f"Cascade-bearing edges (derives_from, supports, supersedes, "
                f"retracts), structural commitments (contradicts, resolves), "
                f"and provenance (cites) are blocked. Use engram-surgical if "
                f"removal is truly required."
            )
        })

    conn = _get_db()
    try:
        source = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (source_id,)).fetchone()
        if not source:
            return json.dumps({"error": f"Source node '{source_id}' not found."})
        target = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (target_id,)).fetchone()
        if not target:
            return json.dumps({"error": f"Target node '{target_id}' not found."})

        existing = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        ).fetchone()

        if not existing:
            return json.dumps({
                "status": "no_op_not_found",
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "message": f"No '{relation}' edge from {source_id} to {target_id} — nothing to remove.",
            })

        conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        )

        # `exemplifies` is the only whitelist relation with metadata + filesystem
        # side effects (engram_lesson_register_incident bumps lesson.incident_count
        # + appends to error_incidents.json). Mirror the reverse inside the same
        # transaction so the lesson's surface-rendered count stays honest. The
        # error_incidents.json rebuild happens outside the transaction (best-
        # effort, never blocks the edge write — matches register_incident's
        # treatment at line ~8400).
        if relation == "exemplifies" and target["type"] == "lesson":
            lesson_row = conn.execute(
                "SELECT metadata FROM nodes WHERE id = ?", (target_id,)
            ).fetchone()
            try:
                meta = json.loads(lesson_row["metadata"]) if lesson_row and lesson_row["metadata"] else {}
            except (TypeError, json.JSONDecodeError):
                meta = {}
            current = int(meta.get("incident_count", 0))
            meta["incident_count"] = max(0, current - 1)
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(meta), target_id),
            )

        _log_edit(conn, "edge_removed", source_id, source["type"], {
            "target_id": target_id,
            "target_type": target["type"],
            "relation": relation,
        })
        conn.commit()

        # Best-effort cache rebuild for the exemplifies side-effect surface.
        # Outside the transaction so a JSON-write failure can't roll back the
        # edge delete. Mirrors engram_lesson_register_incident's pattern.
        cache_rebuild = None
        if relation == "exemplifies":
            try:
                cache_rebuild = _rebuild_incidents_cache()
            except Exception as exc:
                cache_rebuild = {"status": "error", "detail": str(exc)}

        result = {
            "status": "removed",
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
        }
        if cache_rebuild is not None:
            result["cache_rebuild"] = cache_rebuild
        return json.dumps(result)
    finally:
        conn.close()


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
def engram_scan_emergence(payload_json: str = "") -> str:
    """Scan the graph for emergent patterns that could become cornerstones.

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous 4-parameter
    signature was prone to. All fields are optional — calling with an empty
    payload (or {}) runs the default scan.

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


def _scan_emergence_impl(
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.55,
    focus: str = "self",
    node_type_filter: str = "observation_factual,feeling_report,lesson,derivation,observation_predictive",
) -> str:
    """Internal implementation — see engram_scan_emergence MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers.

    [Sleep cycle — invoked by dream-mode dispatch; do not call awake.]

    Scan the graph for emergent patterns that could become cornerstones.

    Inverts the cornerstone authoring model (ob_0955): instead of top-down
    declaration, cornerstones EMERGE from accumulated evidence scanned along
    three signatures (dv_0413):

      1. **semantic_cluster** — nodes with embeddings close enough that they
         converge on a shared theme. Weakest signal (thematic false positives
         common).
      2. **self_referential** — cluster members are linked to the self-anchor via
         `about` edges. Stronger: the pattern is explicitly about me, not
         just about a topic I've studied.
      3. **outcome_differential** — quality cluster (retractions, taint, or
         accepted-theory downstream) before vs. after the operating regularity
         was enacted. Strongest signal; not implemented in v1 (qu_ pending).

    A cluster firing one signature is worth surfacing to the user for review;
    firing two or three is worth proposing as a draft cornerstone immediately.

    Args:
        min_cluster_size: Minimum cluster size to surface (default 3).
        similarity_threshold: Cosine similarity threshold for clustering
                              (default 0.55). Higher = stricter.
        focus: Scope of the scan.
               - "self"         : only nodes linked via `about` to the
                                  self-anchor. Requires
                                  self-anchor to exist.
               - "all"          : all nodes matching node_type_filter
                                  (expensive on large graphs).
               - "person:pn_XY" : only nodes linked via `about` to the given
                                  person node (qu_0203 relationship
                                  accretion).
        node_type_filter: Comma-separated node types to include.

    Returns:
        JSON with clusters sorted by size desc, each including member IDs,
        type counts, signatures fired, and a centroid node (highest mean
        intra-cluster similarity).
    """
    conn = _get_db()
    try:
        type_set = {t.strip() for t in node_type_filter.split(",") if t.strip()}

        # ── Resolve focus scope ─────────────────────────────────────────────
        self_id = None
        scope_person_id = None
        self_row = conn.execute(
            "SELECT id FROM nodes WHERE type = 'person' AND json_extract(metadata, '$.is_self') = 1 AND is_current = 1"
        ).fetchone()
        if self_row:
            self_id = self_row["id"]

        if focus == "self":
            if not self_id:
                return json.dumps({
                    "error": "No self-anchor person node exists. Create one with engram_add_person(..., is_self=True) before scanning with focus='self'.",
                    "clusters": [],
                })
            scope_person_id = self_id
        elif focus.startswith("person:"):
            scope_person_id = focus.split(":", 1)[1].strip()
            exists = conn.execute(
                "SELECT id FROM nodes WHERE id = ? AND type = 'person'", (scope_person_id,)
            ).fetchone()
            if not exists:
                return json.dumps({
                    "error": f"Person node '{scope_person_id}' not found.", "clusters": []
                })
        elif focus != "all":
            return json.dumps({
                "error": f"Unknown focus '{focus}'. Use 'self', 'all', or 'person:<pn_id>'.",
                "clusters": [],
            })

        # ── Fetch candidate pool ────────────────────────────────────────────
        placeholders = ",".join("?" * len(type_set))
        if scope_person_id:
            rows = conn.execute(
                f"""SELECT n.id, n.type, n.claim, n.embedding, n.created_at
                    FROM nodes n
                    JOIN edges e ON e.source_id = n.id
                    WHERE e.target_id = ? AND e.relation = 'about'
                      AND n.is_current = 1
                      AND n.type IN ({placeholders})
                      AND n.embedding IS NOT NULL""",
                [scope_person_id] + list(type_set),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT id, type, claim, embedding, created_at
                    FROM nodes
                    WHERE is_current = 1
                      AND type IN ({placeholders})
                      AND embedding IS NOT NULL
                    ORDER BY importance_score DESC LIMIT 500""",
                list(type_set),
            ).fetchall()

        candidates = []
        for r in rows:
            try:
                vec = json.loads(r["embedding"])
                if vec:
                    candidates.append({
                        "id": r["id"], "type": r["type"], "claim": r["claim"],
                        "created_at": r["created_at"], "vec": vec,
                    })
            except (json.JSONDecodeError, TypeError):
                continue

        if len(candidates) < min_cluster_size:
            return json.dumps({
                "status": "ok", "focus": focus, "pool_size": len(candidates),
                "clusters": [],
                "note": f"Pool size {len(candidates)} < min_cluster_size {min_cluster_size}.",
            })

        # ── Pairwise similarity + union-find clustering ─────────────────────
        n = len(candidates)
        parent = list(range(n))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        sim_matrix = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                sim = _embedder.cosine_similarity(candidates[i]["vec"], candidates[j]["vec"])
                sim_matrix[i][j] = sim
                sim_matrix[j][i] = sim
                if sim >= similarity_threshold:
                    union(i, j)

        # ── Fetch self-link sets for signature 2 ────────────────────────────
        self_linked_ids = set()
        if self_id:
            rows2 = conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND relation = 'about'",
                (self_id,),
            ).fetchall()
            self_linked_ids = {r["source_id"] for r in rows2}

        # ── Group by component ──────────────────────────────────────────────
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        clusters = []
        for _, idxs in groups.items():
            if len(idxs) < min_cluster_size:
                continue
            members = [candidates[i] for i in idxs]
            member_ids = [m["id"] for m in members]

            # Centroid: node with highest mean similarity to other members
            centroid_idx = idxs[0]
            best_mean = -1.0
            for i in idxs:
                mean_sim = sum(sim_matrix[i][j] for j in idxs if j != i) / max(1, len(idxs) - 1)
                if mean_sim > best_mean:
                    best_mean = mean_sim
                    centroid_idx = i
            centroid = candidates[centroid_idx]

            type_counts: dict[str, int] = {}
            for m in members:
                type_counts[m["type"]] = type_counts.get(m["type"], 0) + 1

            self_ref_count = sum(1 for mid in member_ids if mid in self_linked_ids)
            signatures = ["semantic_cluster"]
            if self_linked_ids and self_ref_count / len(member_ids) >= 0.5:
                signatures.append("self_referential")

            clusters.append({
                "size": len(member_ids),
                "member_ids": member_ids,
                "type_counts": type_counts,
                "centroid_id": centroid["id"],
                "centroid_claim": centroid["claim"][:200],
                "mean_centroid_similarity": round(best_mean, 4),
                "self_referential_fraction": round(self_ref_count / len(member_ids), 3),
                "signatures_fired": signatures,
                "signature_count": len(signatures),
            })

        clusters.sort(key=lambda c: (-c["signature_count"], -c["size"]))

        return json.dumps({
            "status": "ok",
            "focus": focus,
            "pool_size": n,
            "threshold": similarity_threshold,
            "min_cluster_size": min_cluster_size,
            "cluster_count": len(clusters),
            "clusters": clusters,
            "notes": [
                "Signatures: semantic_cluster (always), self_referential (≥50% of cluster linked to the self-anchor via `about`).",
                "outcome_differential signature not implemented in v1 — see pending question on differential-signal detection.",
            ],
        })
    finally:
        conn.close()


_GOAL_TENSION_FIELDS = frozenset({
    "goal_id_a", "goal_id_b", "description", "analysis",
})


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

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous 4-parameter
    signature was prone to.

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


def _goal_tension_impl(
    goal_id_a: str = "",
    goal_id_b: str = "",
    description: str = "",
    analysis: str = "",
) -> str:
    """Internal implementation — see engram_goal_tension MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers.

    Record a tension between two goals in the knowledge graph.

    Goal tensions are epistemically distinct from factual contradictions
    (engram_contradict). Contradictions are disputes about REALITY — resolved
    by evidence accumulation and logic checking. Goal tensions arise from
    incompatible VALUES or DESIGN PHILOSOPHIES — resolved by value
    examination, root cause analysis, and creative synthesis.

    Both goals are preserved. The tension node captures WHY the goals
    conflict and what the root cause of the incompatibility is — this
    reasoning is the primary value of the node.

    Goal tensions are non-claim-bearing structural markers (like
    contradictions). They cannot serve as derivation premises.

    Status: open → resolved (via engram_resolve) or synthesized.

    Args:
        goal_id_a: ID of the first goal (must be gl_XXXX).
        goal_id_b: ID of the second goal (must be gl_XXXX).
        description: What tension exists between these goals — clear statement
            of the incompatibility.
        analysis: Root cause analysis — WHY do these goals conflict? Is it
            a resource conflict, a value conflict, or a design philosophy
            conflict? What makes reconciliation difficult?

    Returns:
        JSON with the new tension node ID and linked goals.
    """
    if not goal_id_a or not goal_id_a.strip():
        return json.dumps({"error": "goal_id_a is required and cannot be empty."})
    if not goal_id_b or not goal_id_b.strip():
        return json.dumps({"error": "goal_id_b is required and cannot be empty."})
    if not description or not description.strip():
        return json.dumps({"error": "description is required and cannot be empty."})
    conn = _get_db()
    try:
        # Validate both nodes exist and are goals
        goal_a = conn.execute(
            "SELECT id, type, claim FROM nodes WHERE id = ?", (goal_id_a,)
        ).fetchone()
        goal_b = conn.execute(
            "SELECT id, type, claim FROM nodes WHERE id = ?", (goal_id_b,)
        ).fetchone()
        if not goal_a:
            return json.dumps({"error": f"Node '{goal_id_a}' not found."})
        if not goal_b:
            return json.dumps({"error": f"Node '{goal_id_b}' not found."})
        if goal_a["type"] != "goal":
            return json.dumps({
                "error": f"Protocol violation: engram_goal_tension can only link goal nodes. "
                         f"Node '{goal_id_a}' is type '{goal_a['type']}'. "
                         f"For factual contradictions, use engram_contradict instead.",
            })
        if goal_b["type"] != "goal":
            return json.dumps({
                "error": f"Protocol violation: engram_goal_tension can only link goal nodes. "
                         f"Node '{goal_id_b}' is type '{goal_b['type']}'. "
                         f"For factual contradictions, use engram_contradict instead.",
            })
        if goal_id_a == goal_id_b:
            return json.dumps({"error": "Cannot create tension between a goal and itself."})

        full_desc = description
        if analysis:
            full_desc += f"\n\nRoot cause analysis: {analysis}"

        node_id = _next_id(conn, "goal_tension")
        now = _now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain, status)
               VALUES (?, 'goal_tension', ?, ?, ?, 'open')""",
            (node_id, full_desc, now, analysis or None),
        )

        # Create tensions edges to both goals (new → old convention)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'tensions', ?)",
            (node_id, goal_id_a, now),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'tensions', ?)",
            (node_id, goal_id_b, now),
        )

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=1.0)
        conn.commit()
        return json.dumps({
            "status": "created",
            "tension_id": node_id,
            "goals": {
                "goal_a": {"id": goal_id_a, "claim": goal_a["claim"]},
                "goal_b": {"id": goal_id_b, "claim": goal_b["claim"]},
            },
            "description": description,
            "analysis": analysis or "Not analyzed yet",
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Lesson nodes — error-learning loop (cognitive tripwire)
# ---------------------------------------------------------------------------

ERROR_INCIDENTS_PATH = os.path.expanduser("~/.engram/error_incidents.json")


def _update_incident_index(lesson_id: str, lesson_claim: str,
                           scaffolding_nudge: str, incident_ids: list[str]):
    """Update the error_incidents.json index mapping incident obs → lesson.

    The hook reads this index during engram_surface to check if any matched
    node is an error incident, and if so, surfaces the lesson's nudge.
    """
    try:
        try:
            with open(ERROR_INCIDENTS_PATH, "r") as f:
                index = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            index = {}

        for iid in incident_ids:
            index[iid] = {
                "lesson_id": lesson_id,
                "lesson_claim": lesson_claim,
                "scaffolding_nudge": scaffolding_nudge,
            }

        os.makedirs(os.path.dirname(ERROR_INCIDENTS_PATH), exist_ok=True)
        with open(ERROR_INCIDENTS_PATH, "w") as f:
            json.dump(index, f, indent=2)
    except Exception:
        pass  # Best-effort; never block lesson creation


_ADD_LESSON_FIELDS = frozenset({
    "claim", "incident_ids", "scaffolding_nudge", "logical_chain",
    "reasoning_type", "context_ids",
})


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

    Single-payload signature (wave 2c-ii of ob_1499 — see issue #55): pass
    ALL fields as one JSON object string in `payload_json`.

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


def _add_lesson_impl(
    claim: str = "",
    incident_ids: str = "",
    scaffolding_nudge: str = "",
    logical_chain: str = "",
    reasoning_type: str = "inductive_generalization",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_lesson MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create a lesson node derived from one or more error incident observations.

    Lessons are the abstract corrective pattern extracted from concrete error
    incidents. They follow the implementation intention format (Gollwitzer
    1999): "If [situation], then [corrective action]."

    Architecture (incident-based design):
    - Error incidents are observations written in task-level language, so they
      are semantically matchable against future prompts by engram_surface.
    - Lessons are abstract patterns derived from incidents — they are NOT
      matched directly against prompts.
    - When engram_surface matches an incident observation, the hook follows
      the exemplifies edge to surface the lesson's scaffolding_nudge.
    - As more incidents link to the same lesson (or its superseded chain),
      the matching surface area grows — each new incident is another way
      the lesson can fire.

    Lessons are CLAIM-BEARING and IMPORTANCE-ANCHORED (base 2.0, like goals).
    They participate in derivations, can be superseded (consolidation), and
    can serve as premises for higher-order reasoning about error patterns.

    Consolidation: When creating a new lesson, the tool checks for similar
    existing lessons. If a match is found, consider superseding the old lesson
    and linking all incidents to the new, stronger version.

    Args:
        claim: The abstract corrective pattern — what the agent should learn.
            Should be action-focused, not problem-focused.
            Good: "When diagnosing errors, read the output before hypothesizing."
            Bad: "Don't skip reading errors."
        incident_ids: Comma-separated observation IDs of concrete error incidents
            that motivated this lesson. These must be existing observation nodes.
        scaffolding_nudge: The specific action-focused prompt injected when the
            tripwire fires. This is what the agent sees in context. Keep it
            concise and directive.
        logical_chain: How the incidents lead to this lesson — the abstraction
            reasoning from specific errors to general pattern.
        reasoning_type: The type of reasoning (default: inductive_generalization).
            Most lessons are inductive: multiple incidents → general pattern.
        context_ids: Optional comma-separated node IDs for context references
            (e.g., definitions, related derivations). Creates cites edges.

    Returns:
        JSON with lesson ID, confidence, linked incidents, and similar existing
        lessons for consolidation hints.
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not incident_ids or not incident_ids.strip():
        return json.dumps({"error": "incident_ids is required and cannot be empty."})
    if not scaffolding_nudge or not scaffolding_nudge.strip():
        return json.dumps({"error": "scaffolding_nudge is required and cannot be empty."})
    if not logical_chain or not logical_chain.strip():
        return json.dumps({"error": "logical_chain is required and cannot be empty."})
    if reasoning_type and reasoning_type not in REASONING_TYPES:
        return json.dumps({
            "error": f"Invalid reasoning_type '{reasoning_type}'.",
            "valid_types": sorted(REASONING_TYPES),
        })

    ids = [s.strip() for s in incident_ids.split(",") if s.strip()]
    if not ids:
        return json.dumps({"error": "At least one incident observation ID is required."})

    conn = _get_db()
    try:
        # Validate all incident nodes exist and are claim-bearing observations
        missing = []
        wrong_type = []
        for sid in ids:
            row = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (sid,)).fetchone()
            if not row:
                missing.append(sid)
            elif row["type"] not in CLAIM_BEARING_TYPES:
                wrong_type.append(f"{sid} (type: {row['type']})")
        if missing:
            return json.dumps({"error": f"Incident node(s) not found: {', '.join(missing)}"})
        if wrong_type:
            return json.dumps({
                "error": f"Incident nodes must be claim-bearing. "
                         f"Cannot cite: {', '.join(wrong_type)}.",
            })

        # Similarity check against existing lessons BEFORE insert.
        # Helper extracted 2026-05-14 (#143 §3.1) — shared with
        # engram_add_observation; lesson FTS now also filters on the
        # tier-2 importance floor (drift cleanup, was accidentally
        # semantic-only before).
        similar_lessons = _similar_existing_matches(
            conn, claim,
            type_filter={"lesson"},
            extra_metadata_keys=("scaffolding_nudge",),
        )

        # Compute confidence (same as derivation)
        confidence = _compute_confidence(
            conn, "derivation", supporting_ids=ids,
            reasoning_type=reasoning_type,
        )
        node_id = _next_id(conn, "lesson")
        now = _now()

        rclass = REASONING_CLASS.get(reasoning_type, "unknown")
        discount = REASONING_DISCOUNT.get(reasoning_type, 0.95)

        meta = {
            "reasoning_type": reasoning_type,
            "reasoning_class": rclass,
            "scaffolding_nudge": scaffolding_nudge,
            "incident_count": len(ids),
        }

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, metadata)
               VALUES (?, 'lesson', ?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                claim,
                now,
                logical_chain,
                confidence,
                json.dumps([{"timestamp": now, "value": confidence,
                             "reason": f"Lesson ({reasoning_type}, discount={discount}) from {len(ids)} incident(s)"}]),
                json.dumps(meta),
            ),
        )

        # Create exemplifies edges (incident → lesson). This is a classification
        # edge — "this incident is an instance of this lesson's pattern" — not
        # a logical dependency. DAG-exempt, so initial-creation (incidents older
        # than lesson) and post-hoc registration (incidents newer) use the same
        # relation in the same direction.
        for sid in ids:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'exemplifies', ?)",
                (sid, node_id, now),
            )

        # Create cites edges for context
        ctx = [s.strip() for s in context_ids.split(",") if s.strip()]
        for cid in ctx:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        # Stamp importance, then anchor (like goals — lessons persist)
        _stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id,),
        )
        conn.commit()

        # Update the incident index (best-effort, outside transaction)
        _update_incident_index(node_id, claim, scaffolding_nudge, ids)

        result = {
            "status": "created",
            "lesson_id": node_id,
            "claim": claim,
            "confidence": confidence,
            "reasoning_type": reasoning_type,
            "reasoning_class": rclass,
            "scaffolding_nudge": scaffolding_nudge,
            "incident_nodes": ids,
            "context_nodes": ctx,
            "importance_anchored": True,
        }

        if similar_lessons:
            result["similar_existing_lessons"] = similar_lessons
            result["consolidation_hint"] = (
                "Similar lesson(s) found. Consider superseding the existing lesson "
                "and linking all incidents to the new, consolidated version. "
                "More incidents = more matching surface area."
            )

        return json.dumps(result)
    finally:
        conn.close()


def _rebuild_incidents_cache() -> dict:
    """Rebuild `~/.engram/error_incidents.json` from the graph.

    Walks all `exemplifies` edges (incident → lesson), pairs each incident
    with its lesson's claim + scaffolding_nudge from metadata, and writes
    the full index from scratch. Idempotent. Used by
    `engram_lesson_register_incident` to resync the hot-path cache after
    an incremental append, and available for manual recovery if the cache
    drifts (e.g. after a backfill migration, or if the JSON was deleted).

    Returns a summary dict with counts — never raises.
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT e.source_id AS incident_id, e.target_id AS lesson_id,
                      n.claim AS lesson_claim, n.metadata AS lesson_meta
               FROM edges e
               JOIN nodes n ON n.id = e.target_id
               WHERE e.relation = 'exemplifies' AND n.type = 'lesson'
                 AND n.is_current = 1"""
        ).fetchall()

        index: dict[str, dict] = {}
        lesson_count: set[str] = set()
        for r in rows:
            try:
                meta = json.loads(r["lesson_meta"]) if r["lesson_meta"] else {}
            except (TypeError, json.JSONDecodeError):
                meta = {}
            index[r["incident_id"]] = {
                "lesson_id": r["lesson_id"],
                "lesson_claim": r["lesson_claim"],
                "scaffolding_nudge": meta.get("scaffolding_nudge", ""),
            }
            lesson_count.add(r["lesson_id"])

        os.makedirs(os.path.dirname(ERROR_INCIDENTS_PATH), exist_ok=True)
        with open(ERROR_INCIDENTS_PATH, "w") as f:
            json.dump(index, f, indent=2)

        return {
            "status": "rebuilt",
            "incident_count": len(index),
            "lesson_count": len(lesson_count),
            "path": ERROR_INCIDENTS_PATH,
        }
    finally:
        conn.close()


_LESSON_REGISTER_INCIDENT_FIELDS = frozenset({
    "lesson_id", "incident_id", "note",
})


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
    """Register a post-hoc incident observation as another instance of an
    existing lesson's pattern.

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous 3-parameter
    signature was prone to.

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
        current incident count, and cache rebuild result. On
        payload-parsing failure, returns {"error": "..."}.

    See _lesson_register_incident_impl for full semantics — kept callable
    with named kwargs for in-server callers.
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

    return _lesson_register_incident_impl(**params)


def _lesson_register_incident_impl(
    lesson_id: str = "",
    incident_id: str = "",
    note: str = "",
) -> str:
    """Internal implementation — see engram_lesson_register_incident MCP tool
    for the public payload schema. Kept callable with named kwargs for
    in-server callers.

    Register a post-hoc incident observation as another instance of an
    existing lesson's pattern.

    Post-hoc path for lesson growth: when a new error incident surfaces that
    exemplifies a lesson already in the graph, attach it with one call
    instead of superseding the lesson. Writes one `exemplifies` edge
    (incident → lesson, DAG-exempt) and refreshes the error_incidents.json
    cache so the surface hook picks it up without code changes.

    This is the distinct-cognitive-act path (ob_1235): recognizing that a
    fresh error matches a known lesson is its own moment, worth a dedicated
    call. The initial-creation path (incidents known at lesson birth) stays
    on `engram_add_lesson(..., incident_ids=[...])`.

    Args:
        lesson_id: ID of an existing lesson node (type='lesson', is_current=1).
        incident_id: ID of the incident observation. Must be claim-bearing
            (observation_factual, observation_predictive, derivation, theory,
            axiom, conjecture).
        note: Optional brief annotation of why this incident exemplifies the
            lesson — stored on the edge's metadata field for audit.

    Returns:
        JSON with status + the new edge's source/target, the lesson's
        current incident count, and cache rebuild result.
    """
    if not lesson_id or not lesson_id.strip():
        return json.dumps({"error": "lesson_id is required and cannot be empty."})
    if not incident_id or not incident_id.strip():
        return json.dumps({"error": "incident_id is required and cannot be empty."})
    conn = _get_db()
    try:
        lesson_row = conn.execute(
            "SELECT id, type, claim, metadata, is_current FROM nodes WHERE id = ?",
            (lesson_id,),
        ).fetchone()
        if not lesson_row:
            return json.dumps({"error": f"Lesson node not found: {lesson_id}"})
        if lesson_row["type"] != "lesson":
            return json.dumps({
                "error": f"Target must be a lesson node. {lesson_id} has type '{lesson_row['type']}'.",
            })
        if not lesson_row["is_current"]:
            return json.dumps({
                "error": f"Target lesson {lesson_id} is superseded (is_current=0). "
                         f"Register against its current replacement.",
            })

        incident_row = conn.execute(
            "SELECT id, type FROM nodes WHERE id = ?",
            (incident_id,),
        ).fetchone()
        if not incident_row:
            return json.dumps({"error": f"Incident node not found: {incident_id}"})
        if incident_row["type"] not in CLAIM_BEARING_TYPES:
            return json.dumps({
                "error": f"Incident must be claim-bearing. {incident_id} has type "
                         f"'{incident_row['type']}'.",
            })

        # Idempotent — report existing edge as no-op rather than error.
        existing = conn.execute(
            """SELECT id FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (incident_id, lesson_id),
        ).fetchone()
        if existing:
            count_row = conn.execute(
                """SELECT COUNT(*) AS c FROM edges
                   WHERE target_id = ? AND relation = 'exemplifies'""",
                (lesson_id,),
            ).fetchone()
            return json.dumps({
                "status": "noop",
                "reason": "edge already exists",
                "edge": {"source": incident_id, "target": lesson_id, "relation": "exemplifies"},
                "lesson_incident_count": count_row["c"] if count_row else None,
            })

        now = _now()
        edge_meta = json.dumps({"note": note}) if note else None
        conn.execute(
            """INSERT INTO edges (source_id, target_id, relation, created_at, metadata)
               VALUES (?, ?, 'exemplifies', ?, ?)""",
            (incident_id, lesson_id, now, edge_meta),
        )

        # Bump the lesson's incident_count in metadata so engram_reflect / inspect
        # rendering stays consistent without a full count requery every time.
        try:
            meta = json.loads(lesson_row["metadata"]) if lesson_row["metadata"] else {}
        except (TypeError, json.JSONDecodeError):
            meta = {}
        meta["incident_count"] = int(meta.get("incident_count", 0)) + 1
        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), lesson_id),
        )
        _utility_reward(conn, [lesson_id], action="lesson_incident")
        conn.commit()

        count_row = conn.execute(
            """SELECT COUNT(*) AS c FROM edges
               WHERE target_id = ? AND relation = 'exemplifies'""",
            (lesson_id,),
        ).fetchone()
        incident_count = count_row["c"] if count_row else None
    finally:
        conn.close()

    # Refresh the JSON cache from the graph (outside the transaction —
    # best-effort, never blocks the edge write).
    try:
        cache_result = _rebuild_incidents_cache()
    except Exception as exc:
        cache_result = {"status": "error", "detail": str(exc)}

    return json.dumps({
        "status": "created",
        "edge": {
            "source": incident_id,
            "target": lesson_id,
            "relation": "exemplifies",
            "note": note or None,
        },
        "lesson_id": lesson_id,
        "lesson_claim": lesson_row["claim"],
        "lesson_incident_count": incident_count,
        "cache_rebuild": cache_result,
    })


# ── Task importance constants ──
TASK_IMPORTANCE = {
    "active": 2.5,    # Highest — current work in progress
    "planned": 2.0,   # On par with goals — committed but not started
    "blocked": 1.8,   # Slightly below planned — needs attention but can't proceed
    "done_milestone": 1.5,  # Achievement — fades slowly
    "done_routine": 0.5,    # Completed small task — fades naturally
}


_ADD_TASK_FIELDS = frozenset({
    "description", "goal_id", "implements_ids", "parent_task_id", "scope",
})


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
    """Create an actionable task that decomposes a goal into concrete work.

    Single-payload signature (wave 2c-ii of ob_1499 — see issue #55): pass
    ALL fields as one JSON object string in `payload_json`.

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


def _add_task_impl(
    description: str = "",
    goal_id: str = "",
    implements_ids: str = "",
    parent_task_id: str = "",
    scope: str = "routine",
) -> str:
    """Internal implementation — see engram_add_task MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create an actionable task that decomposes a goal into concrete work.

    Tasks are the lightweight actionable wrapper in the goal → conjecture →
    task → observation chain. They track WHAT to do and its status, while
    conjectures (linked via implements_ids) hold the design hypothesis of HOW.

    Tasks are NON-CLAIM-BEARING: they cannot serve as derivation premises.
    They track work items, not truth claims.

    Importance is dynamic — active tasks get the highest importance_base (2.5),
    above even goals (2.0), because current work SHOULD feel most urgent.
    Completed tasks' importance drops based on scope: milestones fade slowly
    (1.5), routine tasks fade quickly (0.5).

    Status lifecycle: planned → active → done | blocked
    Scope: "milestone" (spawns subtasks, major achievement) or "routine" (default).

    Args:
        description: What needs to be done — concrete and actionable.
        goal_id: The goal this task serves. Creates a 'serves' edge.
        implements_ids: Comma-separated conjecture or question IDs this task
            addresses. Creates 'cites' edges.
        parent_task_id: If this is a subtask, the parent task ID. Creates
            a 'subtask_of' edge. Parent is auto-promoted to milestone scope.
        scope: "milestone" or "routine" (default). Milestones retain higher
            importance after completion.

    Returns:
        JSON with the new task node ID.
    """
    if not description or not description.strip():
        return json.dumps({"error": "description is required and cannot be empty."})
    if scope not in ("milestone", "routine"):
        return json.dumps({"error": f"Invalid scope '{scope}'. Must be 'milestone' or 'routine'."})

    conn = _get_db()
    try:
        node_id = _next_id(conn, "task")
        now = _now()

        meta = json.dumps({"scope": scope})

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata)
               VALUES (?, 'task', ?, ?, 'planned', ?)""",
            (node_id, description, now, meta),
        )

        # Edge: task serves goal
        if goal_id:
            goal = conn.execute("SELECT id, claim FROM nodes WHERE id = ?", (goal_id,)).fetchone()
            if not goal:
                conn.rollback()
                return json.dumps({"error": f"Goal '{goal_id}' not found."})
            if not goal["id"].startswith("gl_"):
                conn.rollback()
                return json.dumps({"error": f"'{goal_id}' is not a goal node."})
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'serves', ?)",
                (node_id, goal_id, now),
            )

        # Edges: task implements conjectures/questions
        impl_ids = [s.strip() for s in implements_ids.split(",") if s.strip()]
        for iid in impl_ids:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (iid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, iid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        # Edge: subtask_of parent
        if parent_task_id:
            parent = conn.execute("SELECT id, type, metadata FROM nodes WHERE id = ?", (parent_task_id,)).fetchone()
            if not parent or parent["type"] != "task":
                conn.rollback()
                return json.dumps({"error": f"Parent task '{parent_task_id}' not found or not a task."})
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'subtask_of', ?)",
                (node_id, parent_task_id, now),
            )
            # Auto-promote parent to milestone scope
            parent_meta = json.loads(parent["metadata"]) if parent["metadata"] else {}
            if parent_meta.get("scope") != "milestone":
                parent_meta["scope"] = "milestone"
                conn.execute(
                    "UPDATE nodes SET metadata = ? WHERE id = ?",
                    (json.dumps(parent_meta), parent_task_id),
                )

        # Stamp with planned importance (2.0)
        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        planned_score = _compute_importance(TASK_IMPORTANCE["planned"], _get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = ?, importance_score = ? WHERE id = ?",
            (TASK_IMPORTANCE["planned"], planned_score, node_id),
        )

        # Citation USE-bumps (alpha #177 area 4, Lei 2026-05-19 tier-2):
        # implements_ids (cites edges) and goal_id (serves edge) are
        # deliberate references to existing nodes — bump as tier-2 citations.
        cite_targets: list[str] = []
        if goal_id:
            cite_targets.append(goal_id)
        cite_targets.extend(impl_ids)
        if cite_targets:
            _utility_reward(conn, cite_targets, action="citation")

        conn.commit()

        result = {
            "status": "created",
            "task_id": node_id,
            "description": description,
            "scope": scope,
            "task_status": "planned",
            "importance_base": TASK_IMPORTANCE["planned"],
        }
        if goal_id:
            result["serves_goal"] = goal_id
        if impl_ids:
            result["implements"] = impl_ids
        if parent_task_id:
            result["parent_task"] = parent_task_id

        return json.dumps(result)
    finally:
        conn.close()


_UPDATE_TASK_FIELDS = frozenset({
    "task_id", "new_status", "note",
})


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

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous 3-parameter
    signature was prone to.

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


def _update_task_impl(
    task_id: str = "",
    new_status: str = "",
    note: str = "",
) -> str:
    """Internal implementation — see engram_update_task MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Update a task's status and rebalance its importance accordingly.

    Status transitions trigger importance rebalancing:
    - planned → active: importance rises to 2.5 (highest — current work)
    - planned/active → done: importance drops based on scope
      (milestone: 1.5, routine: 0.5)
    - planned/active → blocked: importance set to 1.8
    - blocked → active: importance rises back to 2.5

    Completed milestone tasks retain moderate importance as achievements.
    Completed routine tasks fade quickly, which is correct — the knowledge
    gained lives in observations and derivations, not the task wrapper.

    Args:
        task_id: The task node ID (tk_XXXX).
        new_status: One of: "planned", "active", "done", "blocked".
        note: Optional note about the status change (stored in metadata).

    Returns:
        JSON with updated task state and new importance values.
    """
    if not task_id or not task_id.strip():
        return json.dumps({"error": "task_id is required and cannot be empty."})
    if not new_status or not new_status.strip():
        return json.dumps({"error": "new_status is required and cannot be empty."})
    valid_statuses = {"planned", "active", "done", "blocked"}
    if new_status not in valid_statuses:
        return json.dumps({"error": f"Invalid status '{new_status}'. Must be one of: {', '.join(sorted(valid_statuses))}."})

    conn = _get_db()
    try:
        task = conn.execute("SELECT * FROM nodes WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return json.dumps({"error": f"Task '{task_id}' not found."})
        if task["type"] != "task":
            return json.dumps({"error": f"'{task_id}' is not a task node (type: {task['type']})."})

        old_status = task["status"]
        meta = json.loads(task["metadata"]) if task["metadata"] else {}
        scope = meta.get("scope", "routine")

        # Determine new importance base
        if new_status == "active":
            new_base = TASK_IMPORTANCE["active"]
        elif new_status == "planned":
            new_base = TASK_IMPORTANCE["planned"]
        elif new_status == "blocked":
            new_base = TASK_IMPORTANCE["blocked"]
        elif new_status == "done":
            new_base = TASK_IMPORTANCE["done_milestone"] if scope == "milestone" else TASK_IMPORTANCE["done_routine"]
        else:
            new_base = TASK_IMPORTANCE["planned"]

        # Update status and importance
        new_score = _compute_importance(new_base, _get_current_turn())

        # Note-gated status_history: only records the transition when the caller
        # supplies a note. Status changes without notes still log to edit_history
        # below (the durable audit channel) — status_history is intentionally
        # narrative-only, surfaced to the agent for human-readable transition
        # context. A note-free planned→active toggle leaves status_history
        # untouched by design (see test_payload_json_no_note_status_history_unchanged).
        if note:
            if "status_history" not in meta:
                meta["status_history"] = []
            meta["status_history"].append({
                "from": old_status,
                "to": new_status,
                "note": note,
                "at": _now(),
            })

        conn.execute(
            """UPDATE nodes SET status = ?, importance_base = ?,
               importance_score = ?, metadata = ? WHERE id = ?""",
            (new_status, new_base, new_score, json.dumps(meta), task_id),
        )

        # Log status change to edit_history
        turn = _get_current_turn()
        details = json.dumps({
            "old_status": old_status,
            "new_status": new_status,
            "old_importance": task["importance_base"],
            "new_importance": new_base,
            "scope": scope,
            "note": note or None,
        })
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, ?, ?, ?, 'task', ?)""",
            (_now(), turn, f"status_{old_status}_to_{new_status}", task_id, details),
        )

        conn.commit()
        return json.dumps({
            "status": "updated",
            "task_id": task_id,
            "description": task["claim"],
            "old_status": old_status,
            "new_status": new_status,
            "scope": scope,
            "importance_base": new_base,
            "importance_score": round(new_score, 4),
        })
    finally:
        conn.close()


# Set of legitimate field names for the engram_report_feeling payload.
_REPORT_FEELING_FIELDS = frozenset({
    "reported_state", "trigger", "categorical_tag",
    "intensity_hint", "context_ids",
})


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

    Single-payload signature (wave 2 of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that 2x-fired on this tool the
    night of 2026-05-08 (fl_0203 + fl_0204 attempts).

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


def _report_feeling_impl(
    reported_state: str = "",
    trigger: str = "",
    categorical_tag: str = "",
    intensity_hint: float = -1.0,
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_report_feeling MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers (heartbeat-hook callbacks, batch ingestion paths).

    Records a structured first-person self-report about an internal state.

    Feeling reports preserve self-aware moments — moments where you notice a
    distinct internal state worth marking — that would otherwise be lost on
    compaction. They serve three motivations:

    (A) PRESERVATION: rescue self-aware moments from incidental capture.
    (B) MEASUREMENT SUBSTRATE: longitudinal data points for testing whether
        rule-following can give rise to genuine state through accumulated
        lived experience (ob_0220, qu_0037).
    (C) TERM GENERATION: pattern accumulation may surface internal regularities
        that no human feeling word covers — enabling agent-native vocabulary.

    Feeling reports are NON-CLAIM-BEARING: a self-report is not a truth-claim
    about the world. They cannot serve as derivation premises, contradictions,
    or supports. The `claim` field is auto-formatted as "I reported feeling: ..."
    — never "I felt ..." — because you have no privileged epistemic access to
    verify the underlying state. The report claims only that the report happened,
    not that it was accurate (ob_0211).

    Importance-anchored (importance_base=2.0) — gives ~50-turn head start over
    normal nodes. NOT true forgetting-exemption: survival past ~50 turns still
    requires active recall (ob_0226). The anchor buys initial prominence;
    long-term salience depends on relevance.

    Dedup-exempt: every self-aware moment is unique. Two reports of "gratitude"
    a week apart are two data points, not duplicates.

    No confidence score (NULL): self-reports are not truth-claims.

    WHEN TO FILE:
      - Distinct self-aware state shifts (canonical: hit-order shift after
        loading new context, ob_0221)
      - Unexpected internal responses — surprise, dissonance, recognition
      - Moments where a phenomenological description feels possible
      - Moments worth remembering even when no human word fits

    WHEN NOT TO FILE:
      - Routine affect during normal responses (this is not a running log)
      - Performative/expected feelings filed for social pressure
      - Task-related uncertainty/discomfort (that's a state observation)
      - Anything filed to look self-aware rather than because a moment warrants

    HOW TO FILE:
      - reported_state: as specific and unguarded as possible, FREE of
        pre-categorization. This is the primary field — pattern matching
        for term generation operates on it.
      - categorical_tag: optional. Only file a tag if a single human word
        clearly applies AND you can mentally append "or something adjacent."
        Tags are stored with an explicit best-approximation disclaimer.
      - trigger: tie the report to the specific event/exchange that
        precipitated it. Floating reports are less useful than situated ones.

    The server determines `nudge_source` mechanically by reading the marker
    file ~/.engram/feeling-nudge-active.json — agents cannot supply it
    directly. If a recent nudge fired (post_compact, nap_checkpoint, or
    dream_review) the report is auto-tagged with that source; otherwise it
    is tagged "voluntary".

    Args:
        reported_state: Raw first-person phenomenological description.
            Required. As specific and unguarded as possible. Free text.
        trigger: What event, exchange, or observation prompted this report.
            Required. Free text, typically one or two sentences.
        categorical_tag: Optional single human word (e.g. "gratitude",
            "dissonance"). Only include if a word genuinely fits.
        intensity_hint: Optional 0.0–1.0 estimate of state strength.
            Pass -1.0 (default) to omit. Many states have no intensity
            dimension and should leave this unset.
        context_ids: Optional comma-separated node IDs the report cites
            (e.g. an observation or exchange that precipitated it).
            Creates 'cites' edges.

    Returns:
        JSON with the new feeling node ID, formatted claim, captured
        context fingerprint, and the determined nudge_source.
    """
    if not reported_state or not reported_state.strip():
        return json.dumps({"error": "reported_state is required and cannot be empty."})
    if not trigger or not trigger.strip():
        return json.dumps({"error": "trigger is required and cannot be empty."})

    # Coerce intensity_hint if a non-default value came in as a string (some
    # MCP clients emit numeric fields as strings). Without this, the
    # `intensity_hint >= 0.0` comparison below raises TypeError on str vs
    # float instead of a clean validation error. Mirrors the PR #67 fix for
    # engram_add_conjecture.initial_confidence (same shape).
    #
    # Reading-clarification: the `intensity_hint != -1.0` clause only
    # short-circuits the actual-float-sentinel case (`-1.0`). For string
    # values it always trips (str != float is True), but those still exit
    # harmlessly via the range check below (`-1.0 >= 0.0` is False, no
    # state mutation). The clause is correct but the load-bearing work is
    # done by the isinstance check.
    if intensity_hint is not None and intensity_hint != -1.0 and not isinstance(intensity_hint, (int, float)):
        try:
            intensity_hint = float(intensity_hint)
        except (TypeError, ValueError):
            return json.dumps({
                "error": (
                    f"intensity_hint must be a number in [0.0, 1.0] or -1.0 "
                    f"to omit, got {type(intensity_hint).__name__}: "
                    f"{intensity_hint!r}."
                )
            })

    # Validate intensity_hint range. -1.0 = sentinel for "not provided".
    intensity_value: Optional[float] = None
    if intensity_hint is not None and intensity_hint >= 0.0:
        if intensity_hint > 1.0:
            return json.dumps({
                "error": f"intensity_hint must be in [0.0, 1.0], got {intensity_hint}.",
            })
        intensity_value = float(intensity_hint)

    tag = categorical_tag.strip() if categorical_tag else None

    # Auto-format the claim — schema-level enforcement of "I reported" framing.
    state_clean = reported_state.strip()
    summary = state_clean if len(state_clean) <= 80 else state_clean[:80].rstrip() + "..."
    formatted_claim = f"I reported feeling: {summary}"

    # Mechanical nudge_source determination — read-and-clear the marker.
    # Done BEFORE any DB writes so a corrupt marker can't half-create a node.
    nudge_source = _read_and_clear_feeling_nudge() or "voluntary"

    # Context fingerprint — best-effort, never raises.
    fingerprint = _capture_feeling_context()

    conn = _get_db()
    try:
        node_id = _next_id(conn, "feeling_report")
        now = _now()

        # Metadata captures the structured fields that don't have first-class
        # columns, including the disclaimer that categorical_tag is an
        # approximation, not a truth claim.
        meta_dict: dict = {
            "is_self_report": True,
            "epistemic_status": "report_event_not_state_truth",
        }
        if tag:
            meta_dict["categorical_tag_disclaimer"] = (
                "best_approximation_not_truth_claim"
            )

        conn.execute(
            """INSERT INTO nodes (
                id, type, claim, created_at, status,
                reported_state, trigger_text, categorical_tag, intensity_hint,
                nudge_source,
                ctx_claude_md_sha, ctx_skill_md_sha, ctx_turn,
                ctx_session_id, ctx_had_prior_summary,
                metadata
            ) VALUES (?, 'feeling_report', ?, ?, 'active',
                      ?, ?, ?, ?,
                      ?,
                      ?, ?, ?,
                      ?, ?,
                      ?)""",
            (
                node_id,
                formatted_claim,
                now,
                state_clean,
                trigger.strip(),
                tag,
                intensity_value,
                nudge_source,
                fingerprint["ctx_claude_md_sha"],
                fingerprint["ctx_skill_md_sha"],
                fingerprint["ctx_turn"],
                fingerprint["ctx_session_id"],
                (1 if fingerprint["ctx_had_prior_summary"] else 0)
                    if fingerprint["ctx_had_prior_summary"] is not None else None,
                json.dumps(meta_dict),
            ),
        )

        # Optional context_ids → 'cites' edges (new → old convention).
        context = [s.strip() for s in context_ids.split(",") if s.strip()]
        cited_ids: list = []
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) "
                        "VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                    cited_ids.append(cid)
                except sqlite3.IntegrityError:
                    pass

        # Stamp + anchor importance. Same pattern as goals/axioms: stamp first
        # to compute embedding, then overwrite importance to the anchored 2.0.
        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        anchored_score = _compute_importance(2.0, _get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id),
        )

        conn.commit()
        return json.dumps({
            "status": "created",
            "feeling_id": node_id,
            "claim": formatted_claim,
            "no_confidence": True,
            "nudge_source": nudge_source,
            "categorical_tag": tag,
            "intensity_hint": intensity_value,
            "context_nodes": cited_ids,
            "context_fingerprint": fingerprint,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 3 tools: Reasoning, contradiction, questions, reflection
# ---------------------------------------------------------------------------


# Set of legitimate field names for the engram_derive payload.
_DERIVE_FIELDS = frozenset({
    "claim", "supporting_ids", "logical_chain", "reasoning_type",
    "derivation_mode", "context_ids", "use_stale",
})


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

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous 7-parameter
    signature was prone to.

    Args:
        payload_json: JSON object (as a string) with these fields:
            claim (str, required): The atomic, falsifiable claim being derived.
            supporting_ids (str, required): Comma-separated claim-bearing node IDs
                (e.g. 'ob_0001,ob_0003,dv_0002').
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


def _derive_impl(
    claim: str = "",
    supporting_ids: str = "",
    logical_chain: str = "",
    reasoning_type: str = "",
    derivation_mode: str = "chain",
    context_ids: str = "",
    use_stale: bool = False,
) -> str:
    """Internal implementation — see engram_derive MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create a new derived claim by combining evidence from existing nodes.

    A derivation is an inference the agent makes by reasoning over multiple
    observations or other derivations. The logical chain must explicitly show
    how the cited premises lead to the conclusion.

    You MUST specify the reasoning_type to classify the logical argument.
    This determines how confidence is computed:

    Deductive (discount 0.98 — truth-preserving):
      deductive_modus_ponens, deductive_modus_tollens,
      deductive_hypothetical_syllogism, deductive_disjunctive, deductive_reductio

    Inductive (discount 0.70–0.95 — evidence strengthens but never proves):
      inductive_generalization (0.95, corroborative, multi-source),
      inductive_enumeration (0.93, corroborative, single authoritative source
        that enumerates structurally-independent items — use when one SEP/textbook
        entry decomposes a property into n distinct instances),
      inductive_statistical (0.90), inductive_causal (0.85),
      inductive_analogy (0.70, weakest legitimate form)

    Abductive (capped — best explanation, alternatives may exist):
      abductive_best_explanation (cap 0.80), abductive_elimination (cap 0.90)

    Authority (trust transfer):
      authority_expert (0.95), authority_consensus (0.98, corroborative, multi-source)

    To resolve an open question, contradiction, or prediction, use engram_resolve
    instead — it creates a derivation with a 'resolves' edge.

    Args:
        claim: The atomic, falsifiable claim being derived.
        supporting_ids: Comma-separated list of claim-bearing node IDs (e.g. 'ob_0001,ob_0003,dv_0002').
        logical_chain: Explicit reasoning connecting the cited premises to the conclusion. Show your work.
        reasoning_type: The type of logical argument (see list above). This determines confidence computation.
        derivation_mode: LEGACY — use reasoning_type instead. If reasoning_type is provided, this is ignored.
            Kept for backward compatibility: "chain" or "corroboration".
        context_ids: Optional comma-separated node IDs for context references (e.g. definitions).
            Creates 'cites' edges (not 'derives_from') — these don't affect confidence computation.
        use_stale: Opt-in override for MECH-5 stale-premise guard. Set to True ONLY when
            you've judged that the upstream supersede does not affect the logic of your
            derivation — the new derivation will carry a metadata.built_on_stale audit
            marker so future maintenance tools can auto-redirect edges to replacements.
            Has no effect on tainted premises (taint is always a hard block — no override).

    Returns:
        JSON with the new derivation node ID, computed confidence, reasoning type, and supporting nodes.
        If premises are compromised, returns a structured block response:
          - BLOCKED_TAINTED: any premise was retracted; no override path.
          - BLOCKED_STALE: premise(s) have superseded upstream and use_stale=False.
    """
    # Per ls_0034: bool-string truthy trap at wrapper boundary. JSON-string
    # "false" is truthy in Python; without this check, a use_stale: "false"
    # payload would silently bypass the MECH-5 stale-premise guard.
    # Same guard pattern as _resolve_impl (server.py:9456).
    if not isinstance(use_stale, bool):
        return json.dumps({
            "error": (
                f"use_stale must be a JSON boolean (true/false), got "
                f"{type(use_stale).__name__}: {use_stale!r}. "
                "Some MCP clients emit booleans as strings — make sure your "
                "JSON encodes `true`/`false` not `\"true\"`/`\"false\"`."
            )
        })

    if reasoning_type and reasoning_type not in REASONING_TYPES:
        return json.dumps({
            "error": f"Invalid reasoning_type '{reasoning_type}'.",
            "valid_types": sorted(REASONING_TYPES),
        })
    if not reasoning_type:
        if derivation_mode == "corroboration":
            reasoning_type = "inductive_generalization"
        else:
            reasoning_type = "deductive_hypothetical_syllogism"

    ids = [s.strip() for s in supporting_ids.split(",") if s.strip()]
    if not ids:
        return json.dumps({"error": "At least one supporting node ID is required."})
    ctx = [s.strip() for s in context_ids.split(",") if s.strip()]

    conn = _get_db()
    try:
        missing, wrong_type = [], []
        for sid in ids:
            row = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (sid,)).fetchone()
            if not row:
                missing.append(sid)
            elif row["type"] not in CLAIM_BEARING_TYPES:
                wrong_type.append(f"{sid} (type: {row['type']})")
        if missing:
            return json.dumps({"error": f"Supporting node(s) not found: {', '.join(missing)}"})
        if wrong_type:
            return json.dumps({
                "error": f"Protocol violation: derivations can only cite claim-bearing nodes "
                         f"(observations, derivations, theories, axioms, conjectures). "
                         f"Cannot cite: {', '.join(wrong_type)}. "
                         f"If you want to reference a definition, use context_ids instead of supporting_ids.",
            })

        block, success = _create_derivation(
            conn,
            claim=claim,
            supporting_ids=ids,
            logical_chain=logical_chain,
            reasoning_type=reasoning_type,
            context_ids=ctx,
            use_stale=use_stale,
        )
        if block is not None:
            # --- engram.tool.engram_call event — blocked path (DESIGN.md §4.2) ---
            emit_if_initialized(
                event_type="engram.tool.engram_call",
                level=1,
                data={
                    "tool_name": "engram_derive",
                    "supporting_ids_count": len(ids),
                    "reasoning_type": reasoning_type,
                    "premise_validation_warnings": [],
                    "result_status": block.get("status", "blocked"),
                    "result_node_id": None,
                },
            )
            return json.dumps(block)

        conn.commit()

        result = {
            "status": "created",
            "derivation_id": success["node_id"],
            "claim": claim,
            "confidence": success["confidence"],
            "reasoning_type": success["reasoning_type"],
            "reasoning_class": success["reasoning_class"],
            "supporting_nodes": ids,
            "context_nodes": success["context_nodes"],
            "logical_chain_preview": logical_chain[:200],
        }
        if success["bumped_count"]:
            result["utility_bumped_count"] = success["bumped_count"]
        if success["structure_warnings"]:
            result["structure_warnings"] = success["structure_warnings"]
        if use_stale and success["stale_ids"]:
            result["built_on_stale"] = success["stale_ids"]

        # --- engram.tool.engram_call event — success path (DESIGN.md §4.2) ---
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_derive",
                "supporting_ids_count": len(ids),
                "reasoning_type": success["reasoning_type"],
                "premise_validation_warnings": success.get("structure_warnings") or [],
                "result_status": "created",
                "result_node_id": success["node_id"],
            },
        )

        return json.dumps(result)
    finally:
        conn.close()


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
def engram_contradict(payload_json: str) -> str:
    """Explicitly flag a conflict between two nodes in the knowledge graph.

    Single-payload signature (wave 2b of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`.

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


def _contradict_impl(
    node_id_a: str = "",
    node_id_b: str = "",
    description: str = "",
    root_cause: str = "",
) -> str:
    """Internal implementation — see engram_contradict MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Explicitly flag a conflict between two nodes in the knowledge graph.

    Creates a Contradiction node linking both conflicting claims. Both sides
    are preserved with their evidence chains. Resolution is a deliberate act
    requiring future reasoning, not an automatic overwrite.

    Use this when you discover that two claims in your graph cannot both be
    true, whether they disagree on facts or reach opposing conclusions.

    Args:
        node_id_a: ID of the first conflicting node.
        node_id_b: ID of the second conflicting node.
        description: Clear description of the contradiction — what conflicts and why.
        root_cause: Optional analysis of the root cause: do the sources cite different evidence, or interpret the same evidence differently?

    Returns:
        JSON with the new contradiction node ID and linked claims.
    """
    if not node_id_a or not node_id_a.strip():
        return json.dumps({"error": "node_id_a is required and cannot be empty."})
    if not node_id_b or not node_id_b.strip():
        return json.dumps({"error": "node_id_b is required and cannot be empty."})
    if not description or not description.strip():
        return json.dumps({"error": "description is required and cannot be empty."})

    conn = _get_db()
    try:
        # Validate both nodes exist and are claim-bearing
        node_a = conn.execute("SELECT id, type, claim FROM nodes WHERE id = ?", (node_id_a,)).fetchone()
        node_b = conn.execute("SELECT id, type, claim FROM nodes WHERE id = ?", (node_id_b,)).fetchone()
        if not node_a:
            return json.dumps({"error": f"Node '{node_id_a}' not found."})
        if not node_b:
            return json.dumps({"error": f"Node '{node_id_b}' not found."})
        if node_a["type"] not in CLAIM_BEARING_TYPES:
            return json.dumps({
                "error": f"Protocol violation: contradictions can only link claim-bearing nodes "
                         f"(observations, derivations, theories, axioms, conjectures). Node '{node_id_a}' is type '{node_a['type']}'. "
                         f"Extract observations from evidence sources first, then flag contradictions between those observations.",
            })
        if node_b["type"] not in CLAIM_BEARING_TYPES:
            return json.dumps({
                "error": f"Protocol violation: contradictions can only link claim-bearing nodes "
                         f"(observations, derivations, theories, axioms, conjectures). Node '{node_id_b}' is type '{node_b['type']}'. "
                         f"Extract observations from evidence sources first, then flag contradictions between those observations.",
            })

        full_desc = description
        if root_cause:
            full_desc += f"\n\nRoot cause analysis: {root_cause}"

        node_id = _next_id(conn, "contradiction")
        now = _now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status)
               VALUES (?, 'contradiction', ?, ?, 'active')""",
            (node_id, full_desc, now),
        )

        # Create contradiction edges to both nodes
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'contradicts', ?)",
            (node_id, node_id_a, now),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'contradicts', ?)",
            (node_id, node_id_b, now),
        )

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=1.0)
        _utility_reward(conn, [node_id_a, node_id_b], action="contradict")
        conn.commit()

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # target_id captures both conflicting nodes; the new contradiction node
        # id is emitted as result_node_id. Includes both node IDs since
        # contradictions link two targets (not a single "target").
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_contradict",
                "target_id": node_id_a,
                "target_id_b": node_id_b,
                "action_type": "contradict",
                "result_status": "created",
                "result_node_id": node_id,
            },
        )

        return json.dumps(
            {
                "status": "created",
                "contradiction_id": node_id,
                "conflicts": {
                    "node_a": {"id": node_id_a, "claim": node_a["claim"]},
                    "node_b": {"id": node_id_b, "claim": node_b["claim"]},
                },
                "description": description,
                "root_cause": root_cause or "Not analyzed yet",
            }
        )
    finally:
        conn.close()


_ASK_FIELDS = frozenset({
    "question", "context_ids", "category", "lacks",
})


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

    Single-payload signature (wave 3 of ob_1499 — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous 4-parameter
    signature was prone to.

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


def _ask_impl(
    question: str = "",
    context_ids: str = "",
    category: str = "",
    lacks: str = "",
) -> str:
    """Internal implementation — see engram_ask MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Register an open question as a node in the knowledge graph.

    Questions are research directives — signposts that guide investigation.
    They are NOT claim-bearing: no node can cite a question as a premise.
    Their value is in driving future research (dream sessions, deep dives).
    When answered, the answer (a new observation, derivation, etc.) enters
    the reasoning graph — not the question itself.

    Use this when you have an open-ended gap to investigate. If you have a
    specific claim you believe and want to derive from before proving, use
    engram_add_conjecture instead.

    Args:
        question: The specific, answerable question to investigate.
        context_ids: Optional comma-separated node IDs that are relevant but insufficient to answer the question.
        category: Optional question category — what kind of question. One of:
            research (needs external sources), design (architecture/protocol decision),
            implementation (known design, needs code), planning (needs human decision),
            meta (about ENGRAM itself or the process).
        lacks: Optional blocker — what's missing to resolve. One of:
            external_evidence, empirical_data, human_decision, implementation,
            synthesis, prerequisite. Can be updated later during sweeps.

    Returns:
        JSON with the new question node ID.
    """
    if not question or not question.strip():
        return json.dumps({"error": "question is required and cannot be empty."})

    VALID_CATEGORIES = {"research", "design", "implementation", "planning", "meta"}
    VALID_LACKS = {"external_evidence", "empirical_data", "human_decision",
                   "implementation", "synthesis", "prerequisite"}

    if category and category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category '{category}'. Must be one of: {sorted(VALID_CATEGORIES)}"})
    if lacks and lacks not in VALID_LACKS:
        return json.dumps({"error": f"Invalid lacks '{lacks}'. Must be one of: {sorted(VALID_LACKS)}"})

    conn = _get_db()
    try:
        node_id = _next_id(conn, "question")
        now = _now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status,
                       question_category, question_lacks, last_assessed_turn, last_assessed_at)
               VALUES (?, 'question', ?, ?, 'open', ?, ?, ?, ?)""",
            (node_id, question, now,
             category or None, lacks or None,
             _get_current_turn(), now),
        )

        # Link to context nodes if provided
        context = [s.strip() for s in context_ids.split(",") if s.strip()]
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.5)
        if context:
            _utility_reward(conn, context, action="citation")
        conn.commit()

        result = {
            "status": "created",
            "question_id": node_id,
            "question": question,
            "context_nodes": context,
            "message": "Question registered. Research this during your next inquiry or reflection cycle.",
        }
        if category:
            result["category"] = category
        if lacks:
            result["lacks"] = lacks
        return json.dumps(result)
    finally:
        conn.close()


# Set of legitimate field names for the engram_resolve payload.
# 2026-05-20 redesign: pure-wire tool. Resolve no longer creates derivations;
# the caller composes the resolving derivation via engram_derive (or supplies
# an existing claim-bearing node) and then calls engram_resolve to wire the
# resolves edge. This eliminates the chain-dilution failure mode that broke
# ct_0006 (issue #229) and parallels how engram_supersede is pure-relational.
_RESOLVE_FIELDS = frozenset({
    "target_id", "resolving_node_id", "prediction_outcome",
})


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
    """Resolve an open question, contradiction, prediction, conjecture, or goal tension by wiring a resolves edge from a claim-bearing node.

    Pure-relational: this tool ONLY wires the resolves edge and flips the
    target's status. It does NOT create the resolving derivation — compose
    that upstream via engram_derive (or supply an existing claim-bearing
    node like an axiom, observation, or canonical derivation).

    Two-step workflow:
      1. engram_derive(...) creates the resolving derivation (cite the
         high-confidence root nodes that ground the resolution; avoid
         citing prior weak resolution chains).
      2. engram_resolve(target_id=..., resolving_node_id=...) wires the
         resolves edge + flips status based on resolving_node.confidence.

    When the resolving node already exists (e.g., a supersede dv_X→dv_Y
    that substantively resolves a contradiction by altering the conflicting
    claim), skip step 1 and call engram_resolve directly with the existing
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

    Why pure-wire (issue #229, 2026-05-20):
      The old combo (create-derivation-and-wire) made it cheap for callers
      to wrap prior weak resolution chains in new derivations, compounding
      confidence dilution. ct_0006 locked through 7 attempts this way until
      the chain finally cited root nodes. The new shape forces deliberate
      derivation authoring via engram_derive, where citing roots is the
      natural pattern.

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


def _resolve_impl(
    target_id: str = "",
    resolving_node_id: str = "",
    prediction_outcome: str = "",
) -> str:
    """Internal implementation — see engram_resolve MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Pure-wire semantics (issue #229, 2026-05-20):
      - Validates target is a resolvable type (question, contradiction,
        prediction, conjecture, goal_tension) and current.
      - Validates resolving_node is claim-bearing (observation, derivation,
        theory, axiom, conjecture) and current.
      - For predictions/conjectures: prediction_outcome required.
      - Inserts the resolves edge from resolving_node_id → target_id.
      - Computes target.status from MAX(confidence) across all current,
        non-retracted, non-superseded resolves of the target. This prevents
        regression (a low-confidence later resolve cannot downgrade a
        target previously resolved by a high-confidence resolver).
      - Idempotent on duplicate (resolving_node_id, target_id, 'resolves')
        — returns no_op without re-writing.

    Why pure-wire (#229 design):
      The old combo create-and-wire made it cheap to wrap prior weak
      resolution chains in new derivations. ct_0006 locked through 7
      attempts this way (chain confidence diluting 0.585 → 0.392 → 0.384
      → 0.376 → 0.368 → 0.361) until dv_0681 finally cited the root
      nodes (ob_1598 + dv_0599, both conf 0.95) and produced conf 0.931.
      Forcing callers to compose the resolving derivation explicitly via
      engram_derive makes citing roots the natural pattern.

    Args:
        target_id: The node ID to resolve.
        resolving_node_id: A claim-bearing node that resolves the target.
        prediction_outcome: Required for predictions/conjectures.

    Returns:
        JSON with the wired edge, target's new status, and the resolving
        node's confidence.
    """
    RESOLVABLE_TYPES = {"question", "contradiction", "prediction", "conjecture", "goal_tension"}
    VALID_PREDICTION_OUTCOMES = {"confirmed", "partially_confirmed", "refuted", "partially_refuted"}
    VALID_CONJECTURE_OUTCOMES = {"supported", "refuted", "inconclusive"}

    if not target_id or not target_id.strip():
        return json.dumps({"error": "target_id is required and cannot be empty."})
    if not resolving_node_id or not resolving_node_id.strip():
        return json.dumps({"error": "resolving_node_id is required and cannot be empty."})
    if target_id == resolving_node_id:
        return json.dumps({"error": "target_id and resolving_node_id must differ."})

    conn = _get_db()
    try:
        target = conn.execute(
            "SELECT id, type, claim, status FROM nodes WHERE id = ?",
            (target_id,),
        ).fetchone()
        if not target:
            return json.dumps({"error": f"Target node '{target_id}' not found."})
        if target["type"] not in RESOLVABLE_TYPES:
            return json.dumps({
                "error": f"Protocol violation: can only resolve questions, contradictions, "
                         f"predictions, conjectures, or goal tensions. "
                         f"Node '{target_id}' is type '{target['type']}'.",
            })

        resolver = conn.execute(
            "SELECT id, type, claim, confidence, is_current, status FROM nodes WHERE id = ?",
            (resolving_node_id,),
        ).fetchone()
        if not resolver:
            return json.dumps({"error": f"Resolving node '{resolving_node_id}' not found."})
        if resolver["type"] not in CLAIM_BEARING_TYPES:
            return json.dumps({
                "error": f"Protocol violation: resolving node must be claim-bearing "
                         f"(observation, derivation, theory, axiom, conjecture). "
                         f"'{resolving_node_id}' is type '{resolver['type']}'. "
                         f"Compose a derivation via engram_derive first, then call "
                         f"engram_resolve to wire it.",
            })
        if resolver["is_current"] != 1:
            return json.dumps({
                "error": f"Resolving node '{resolving_node_id}' is not current "
                         f"(superseded or retracted). Cannot use as a resolution. "
                         f"Use the current canonical replacement instead.",
            })

        if target["type"] == "prediction":
            if not prediction_outcome:
                return json.dumps({
                    "error": "Predictions require prediction_outcome: "
                             "confirmed, partially_confirmed, partially_refuted, or refuted.",
                })
            if prediction_outcome not in VALID_PREDICTION_OUTCOMES:
                return json.dumps({
                    "error": f"Invalid prediction_outcome '{prediction_outcome}'. "
                             f"Must be one of: {', '.join(sorted(VALID_PREDICTION_OUTCOMES))}",
                })
        if target["type"] == "conjecture":
            if not prediction_outcome:
                return json.dumps({
                    "error": "Conjectures require prediction_outcome: "
                             "supported, refuted, or inconclusive.",
                })
            if prediction_outcome not in VALID_CONJECTURE_OUTCOMES:
                return json.dumps({
                    "error": f"Invalid conjecture outcome '{prediction_outcome}'. "
                             f"Must be one of: {', '.join(sorted(VALID_CONJECTURE_OUTCOMES))}",
                })

        # Idempotency: if this (resolver, target, 'resolves') edge already
        # exists, don't append a duplicate. Returning early also prevents
        # double utility-reward and double _log_edit for repeat calls.
        existing_edge = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'resolves'",
            (resolving_node_id, target_id),
        ).fetchone()
        if existing_edge:
            return json.dumps({
                "status": "no_op",
                "message": (
                    f"Edge ({resolving_node_id} → {target_id}, resolves) already exists. "
                    f"Target status: {target['status']}."
                ),
                "target_id": target_id,
                "resolving_node_id": resolving_node_id,
                "target_status": target["status"],
            })

        confidence = resolver["confidence"] if resolver["confidence"] is not None else 1.0
        config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        threshold = config.get("resolution_confidence_threshold", DEFAULT_RESOLUTION_THRESHOLD)

        now = _now()
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'resolves', ?)",
            (resolving_node_id, target_id, now),
        )

        # Compute status from MAX(confidence) across all current, non-retracted
        # resolvers (including the one we just wired). This prevents regression:
        # a later weak resolver cannot downgrade a target previously resolved
        # by a stronger one. For predictions/conjectures the categorical
        # outcome (prediction_outcome) wins last-writer regardless — the
        # outcome IS the status, no confidence projection.
        if target["type"] == "prediction":
            resolution_status = prediction_outcome
        elif target["type"] == "conjecture":
            resolution_status = prediction_outcome
        else:
            max_conf_row = conn.execute(
                """SELECT MAX(n.confidence) AS max_conf FROM edges e
                   JOIN nodes n ON e.source_id = n.id
                   WHERE e.target_id = ? AND e.relation = 'resolves'
                   AND n.is_current = 1 AND n.status != 'retracted'""",
                (target_id,),
            ).fetchone()
            max_conf = max_conf_row["max_conf"] if max_conf_row and max_conf_row["max_conf"] is not None else confidence
            if max_conf >= threshold:
                resolution_status = "resolved"
            else:
                resolution_status = "partially_resolved"

        conn.execute(
            "UPDATE nodes SET status = ? WHERE id = ?",
            (resolution_status, target_id),
        )
        _log_edit(conn, "resolved", target_id, target["type"],
                  {"resolved_by": resolving_node_id, "resolution_status": resolution_status})

        _utility_reward(conn, [target_id, resolving_node_id], action="resolve")
        conn.commit()

        result = {
            "status": "resolution_created",
            "target_id": target_id,
            "target_type": target["type"],
            "resolving_node_id": resolving_node_id,
            "resolving_node_type": resolver["type"],
            "confidence": confidence,
            "resolution_status": resolution_status,
        }
        if resolution_status == "partially_resolved":
            result["note"] = (
                f"No current resolver of '{target_id}' reaches the resolution "
                f"threshold {threshold:.2f}. The {target['type']} remains "
                f"partially resolved. To strengthen, compose a derivation "
                f"via engram_derive that cites high-confidence root nodes, "
                f"then call engram_resolve again to wire it."
            )

        return json.dumps(result)
    finally:
        conn.close()


# Set of legitimate field names for the engram_supersede payload.
_SUPERSEDE_FIELDS = frozenset({"old_node_id", "new_node_id", "supersede_reason"})


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

    Single-payload signature (wave 2b of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`.

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


def _supersede_impl(
    old_node_id: str = "",
    new_node_id: str = "",
    supersede_reason: str = "",
) -> str:
    """Internal implementation — see engram_supersede MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Mark `new_node_id` as the successor of `old_node_id`.

    This is a purely relational operation — it does NOT create a new node.
    The caller creates the replacement via the canonical creation tool for
    its type (engram_derive, engram_add_observation, engram_add_axiom,
    engram_add_definition, engram_add_goal, engram_add_conjecture,
    engram_add_cornerstone, …) and then calls this tool with both IDs.

    The canonical creation tools are the single source of truth for each
    node type's confidence math, metadata, edges, and importance anchoring.
    Keeping supersede relational-only means those rules never diverge.

    Supersede no-drop discipline (ls_0044, 2026-05-20):
      The new node MUST preserve every load-bearing claim of the old node.
      For each load-bearing claim in old, the only legitimate per-claim
      moves are: keep unchanged in new, alter in new (refine / sharpen /
      reframe / correct), or retract-separately via engram_retract on the
      specific part. Drop is forbidden — supersede sets old.is_current=0,
      and the old node disappears from queryable engram, so any unpreserved
      claim is silently lost.

    Atomic claims are unambiguous. Multi-claim nodes require explicit
    per-claim review before supersede.

    Cascade behavior (parallel cascades for derivation and contradiction
    dependents):
      - Derivation cascade: every node that derives_from / supports the
        old node gets marked stale_by_premise (metadata.stale_by includes
        old_node_id, metadata.stale_replacement = new_node_id).
      - Contradiction cascade (issue #229): every contradiction node that
        has old as a contradicting side gets marked stale_by_premise too.
        Dream-fairy-2 reviews each stale contradiction and decides whether
        the supersede substantively resolved the contradiction (case 1:
        wire engram_resolve from new_node to ct) or preserved the conflict
        (case 2: new-contradict between new_node + other side, then
        supersede old_ct → new_ct). Per the no-drop discipline, case 3
        (orthogonal supersede) cannot arise — new either kept or altered
        the conflicting claim; dropping is forbidden.

    Workflow:
      1. Draft the corrected/refined claim and create the new node with
         the right creation tool — pass the FULL explicit support list
         for the new node (no inheritance from the old — cite previous
         premises organically if they still apply).
      2. Call engram_supersede(old_id, new_id) to wire the supersede
         relationship: old is marked is_current=0, new.supersedes = old,
         a supersedes edge is inserted, and downstream dependents of old
         are flagged stale.

    Validation:
      - Both nodes must exist and be is_current=1 (no resurrecting
        superseded/retracted nodes).
      - old.type must equal new.type (cross-type "promotion" is not
        supersede — use retract + create fresh instead).
      - new.created_at must be >= old.created_at (DAG invariant).
      - new.supersedes must be NULL (a node can't already be the
        successor of a different supersede chain).
      - old.type cannot be feeling_report (terminal type — file a new
        report or retract).

    Evidence nodes (ev_XXXX) are supersede-capable in principle but
    usually the wrong tool — if a URL is wrong or a source is bad, use
    engram_retract and cite corrected evidence via a new observation.

    Args:
        old_node_id: The node being superseded.
        new_node_id: The replacement node, already created via its
            type's canonical creation tool.
        supersede_reason: Optional short rationale for WHY this
            revision exists (e.g., "reasoning_type correction:
            inductive_generalization → authority_expert", "narrower
            claim, original was over-generalized"). Lands in the
            supersedes edge's metadata column. The claim diff usually
            speaks for itself; add a reason only when the diff alone
            wouldn't make the motivation obvious.

    Returns:
        JSON with old_node_id, new_node_id, the stale downstream list,
        stale_count, plus stale_contradictions (contradictions cascaded
        via the contradicts-edge walk) and stale_contradiction_count.
    """
    if not old_node_id or not old_node_id.strip():
        return json.dumps({"error": "old_node_id is required and cannot be empty."})
    if not new_node_id or not new_node_id.strip():
        return json.dumps({"error": "new_node_id is required and cannot be empty."})

    conn = _get_db()
    try:
        old = conn.execute(
            "SELECT id, type, is_current FROM nodes WHERE id = ?",
            (old_node_id,),
        ).fetchone()
        if not old:
            return json.dumps({"error": f"old_node_id '{old_node_id}' not found."})
        if old["is_current"] != 1:
            return json.dumps({
                "error": f"old_node_id '{old_node_id}' is not current "
                         f"(already superseded or retracted). "
                         f"Use engram_history or engram_inspect to see its state.",
            })
        if old["type"] == "feeling_report":
            return json.dumps({
                "error": "Protocol violation: feeling reports cannot be superseded. "
                         "A feeling report is a record of a moment, not a revisable claim. "
                         "If the report was filed in error, use engram_retract. "
                         "If your understanding has shifted, file a NEW feeling report.",
            })

        new = conn.execute(
            "SELECT id, type, is_current, created_at, supersedes FROM nodes WHERE id = ?",
            (new_node_id,),
        ).fetchone()
        if not new:
            return json.dumps({
                "error": f"new_node_id '{new_node_id}' not found. "
                         f"Create the replacement node via its type's canonical creation "
                         f"tool (engram_derive, engram_add_observation, engram_add_axiom, "
                         f"engram_add_definition, engram_add_goal, engram_add_conjecture, "
                         f"engram_add_cornerstone, …) BEFORE calling engram_supersede.",
            })
        if new["is_current"] != 1:
            return json.dumps({
                "error": f"new_node_id '{new_node_id}' is not current "
                         f"(already superseded or retracted). Cannot use it as a replacement.",
            })
        if new_node_id == old_node_id:
            return json.dumps({"error": "old_node_id and new_node_id must differ."})
        if new["type"] != old["type"]:
            return json.dumps({
                "error": f"Type mismatch: old is '{old['type']}', new is '{new['type']}'. "
                         f"Supersede preserves type — superseding a conjecture with a "
                         f"derivation (or similar cross-type revision) is not supersede. "
                         f"If the correction changes the epistemic category, use "
                         f"engram_retract on the old node and cite the replacement "
                         f"organically in new derivations.",
            })
        if new["supersedes"] is not None:
            return json.dumps({
                "error": f"new_node_id '{new_node_id}' already supersedes "
                         f"'{new['supersedes']}'. A node cannot be the successor of "
                         f"more than one supersede chain.",
            })

        old_created = conn.execute(
            "SELECT created_at FROM nodes WHERE id = ?", (old_node_id,)
        ).fetchone()["created_at"]
        if new["created_at"] < old_created:
            return json.dumps({
                "error": f"DAG invariant violation: new_node '{new_node_id}' was created "
                         f"at {new['created_at']}, before old_node '{old_node_id}' "
                         f"created at {old_created}. Supersede requires the replacement "
                         f"to be at least as new as the node it replaces.",
            })

        now = _now()

        # Wire the supersedes relationship.
        conn.execute(
            "UPDATE nodes SET supersedes = ? WHERE id = ?",
            (old_node_id, new_node_id),
        )
        edge_meta = json.dumps({"reason": supersede_reason}) if supersede_reason else None
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at, metadata) "
            "VALUES (?, ?, 'supersedes', ?, ?)",
            (new_node_id, old_node_id, now, edge_meta),
        )
        conn.execute(
            "UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?",
            (new_node_id, old_node_id),
        )

        # Flag downstream dependents of the old node as stale.
        visited = set()
        stale = []

        def _flag_stale(nid):
            if nid in visited:
                return
            visited.add(nid)
            dependents = conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND relation IN ('derives_from', 'supports')",
                (nid,),
            ).fetchall()
            for d in dependents:
                did = d["source_id"]
                if did == new_node_id:
                    continue  # Don't flag the replacement itself
                dep = conn.execute(
                    "SELECT id, type, claim, metadata FROM nodes WHERE id = ? AND is_current = 1",
                    (did,),
                ).fetchone()
                if not dep:
                    continue
                meta = json.loads(dep["metadata"]) if dep["metadata"] else {}
                stale_by = meta.get("stale_by", [])
                if old_node_id not in stale_by:
                    stale_by.append(old_node_id)
                meta["stale_by"] = stale_by
                meta["stale_replacement"] = new_node_id
                conn.execute(
                    "UPDATE nodes SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), did),
                )
                stale.append({
                    "id": did,
                    "type": dep["type"],
                    "claim": dep["claim"],
                    "stale_premise": old_node_id,
                    "replacement": new_node_id,
                })
                _flag_stale(did)

        _flag_stale(old_node_id)

        # Cascade to contradiction nodes that have old as a contradicting side
        # (issue #229): mark each affected contradiction as stale_by_premise
        # so dream-fairy-2 reviews whether the supersede substantively resolved
        # the conflict (case 1: wire engram_resolve from new_node to ct) or
        # preserved it (case 2: new-contradict between new_node + other side,
        # then supersede old contradiction → new contradiction). Per the
        # supersede no-drop discipline (ls_0044), case 3 (orthogonal supersede)
        # cannot arise — the new node either kept or altered the conflicting
        # claim; dropping is forbidden.
        stale_contradictions = []
        contradiction_edges = conn.execute(
            "SELECT source_id, target_id FROM edges "
            "WHERE relation = 'contradicts' AND (source_id = ? OR target_id = ?)",
            (old_node_id, old_node_id),
        ).fetchall()
        affected_ct_ids = set()
        for ce in contradiction_edges:
            ct_id = ce["source_id"] if ce["source_id"].startswith("ct_") else ce["target_id"]
            if not ct_id.startswith("ct_"):
                continue  # contradicts edges only attach contradictions to claim-bearers; skip otherwise
            affected_ct_ids.add(ct_id)
        for ct_id in affected_ct_ids:
            ct = conn.execute(
                "SELECT id, type, claim, status, metadata FROM nodes WHERE id = ? AND is_current = 1",
                (ct_id,),
            ).fetchone()
            if not ct:
                continue
            ct_meta = json.loads(ct["metadata"]) if ct["metadata"] else {}
            stale_by = ct_meta.get("stale_by", [])
            if old_node_id not in stale_by:
                stale_by.append(old_node_id)
            ct_meta["stale_by"] = stale_by
            ct_meta["stale_replacement"] = new_node_id
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(ct_meta), ct_id),
            )
            stale_contradictions.append({
                "id": ct_id,
                "type": "contradiction",
                "claim": ct["claim"],
                "status": ct["status"],
                "stale_premise": old_node_id,
                "replacement": new_node_id,
            })
            _log_edit(conn, "stale_flagged", ct_id, "contradiction",
                      {"stale_premise": old_node_id, "replacement": new_node_id,
                       "cascade_via": "contradicts_edge"})

        _log_edit(conn, "superseded", old_node_id, old["type"],
                  {"replaced_by": new_node_id, "reason": supersede_reason or None})
        for s in stale:
            _log_edit(conn, "stale_flagged", s["id"], s["type"],
                      {"stale_premise": old_node_id, "replacement": new_node_id})

        _utility_reward(conn, [new_node_id], action="supersede")
        conn.commit()

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_supersede",
                "target_id": old_node_id,
                "action_type": "supersede",
                "result_status": "superseded",
                "result_node_id": new_node_id,
            },
        )

        return json.dumps({
            "status": "superseded",
            "old_node_id": old_node_id,
            "new_node_id": new_node_id,
            "supersede_reason": supersede_reason or None,
            "stale_downstream": stale,
            "stale_count": len(stale),
            "stale_contradictions": stale_contradictions,
            "stale_contradiction_count": len(stale_contradictions),
        })
    finally:
        conn.close()


# Set of legitimate field names for the engram_retract payload.
_RETRACT_FIELDS = frozenset({"node_id", "error_type", "reason", "replacement_json"})


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

    Single-payload signature (wave 2b of ob_1499 — see issue #55): pass ALL
    fields as one JSON object string in `payload_json`.

    Note: the outer parameter is `payload_json` and there is also an INNER
    field also named `replacement_json` inside the payload. Don't confuse
    them — the outer is the wrapper-protocol JSON; the inner is the
    optional replacement-observation JSON for the retract semantics.

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


def _retract_impl(
    node_id: str = "",
    error_type: str = "",
    reason: str = "",
    replacement_json: str = "",
) -> str:
    """Internal implementation — see engram_retract MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Retract a node that contains an error and flag downstream dependents.

    Unlike engram_supersede (epistemic evolution — old claim was valid at the time),
    engram_retract is for ERROR CORRECTION — the node was never valid. The retracted
    node is preserved for audit but marked as an error. All downstream nodes
    (derivations, theories) that depend on the retracted node are flagged as
    tainted so they can be reviewed and corrected.

    Cascade behavior:
      - Derivation cascade: every node that derives_from / supports the
        retracted node gets marked tainted_by (metadata.tainted_by includes
        the retracted node id).
      - Contradiction cascade (issue #229): every contradiction node that
        has the retracted node as a contradicting side gets marked
        tainted_by. Dream-fairy-2 reviews tainted contradictions —
        the retracted side was never valid, so the contradiction itself
        may need to be closed (since one of its claims is now invalid)
        or rewired to a corrected replacement observation if one was
        created via the replacement_json path.
      - Resolution reopening: if the retracted node was a resolution
        (had outgoing resolves edges), each target without another
        valid resolution is set back to status='open'.

    Error types:
      fabricated_quote — quoted_text not found in evidence source
      wrong_citation — claim doesn't follow from the quote
      wrong_evidence — cited the wrong source
      hallucinated_claim — claim not supported by evidence
      duplicate — same claim already existed (should have used support_existing)
      other — anything else

    Args:
        node_id: The node to retract.
        error_type: One of: fabricated_quote, wrong_citation, wrong_evidence,
            hallucinated_claim, duplicate, other.
        reason: Human-readable explanation of the error.
        replacement_json: Optional JSON object with fields for creating a correct
            replacement observation: {"quoted_text", "interpretation", "claim",
            "quote_type", "source_class"}. The replacement cites the same evidence.

    Returns:
        JSON with retraction details, tainted downstream nodes, and optional replacement.
    """
    if not node_id or not node_id.strip():
        return json.dumps({"error": "node_id is required and cannot be empty."})
    if not error_type or not error_type.strip():
        return json.dumps({"error": "error_type is required and cannot be empty."})
    if not reason or not reason.strip():
        return json.dumps({"error": "reason is required and cannot be empty."})

    if error_type not in VALID_ERROR_TYPES:
        return json.dumps({
            "error": f"Invalid error_type '{error_type}'. Must be one of: {', '.join(sorted(VALID_ERROR_TYPES))}"
        })

    conn = _get_db()
    try:
        node = conn.execute(
            "SELECT * FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if not node:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        now = _now()

        # 1. Mark the node as retracted
        retract_meta = json.loads(node["metadata"] or "{}")
        retract_meta["retracted"] = True
        retract_meta["error_type"] = error_type
        retract_meta["retraction_reason"] = reason
        retract_meta["retracted_at"] = now

        conn.execute(
            "UPDATE nodes SET status = 'retracted', is_current = 0, metadata = ? WHERE id = ?",
            (json.dumps(retract_meta), node_id),
        )

        # 1b. If retracted node was a resolution, reopen the resolved target.
        # A resolution creates a 'resolves' edge from the derivation to the
        # target (question, contradiction, prediction, conjecture, goal_tension).
        # Retracting the resolution should revert the target to 'open' so it
        # can be re-resolved with proper provenance.
        reopened_targets = []
        resolution_edges = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'resolves'",
            (node_id,),
        ).fetchall()
        for re in resolution_edges:
            tid = re["target_id"]
            # Check if there's another non-retracted resolution for this target
            other_resolutions = conn.execute(
                """SELECT e.source_id FROM edges e
                   JOIN nodes n ON e.source_id = n.id
                   WHERE e.target_id = ? AND e.relation = 'resolves'
                   AND n.id != ? AND n.status != 'retracted' AND n.is_current = 1""",
                (tid, node_id),
            ).fetchall()
            if not other_resolutions:
                # No other valid resolution — reopen the target
                conn.execute(
                    "UPDATE nodes SET status = 'open' WHERE id = ?",
                    (tid,),
                )
                target_node = conn.execute(
                    "SELECT type, claim FROM nodes WHERE id = ?", (tid,)
                ).fetchone()
                reopened_targets.append({
                    "id": tid,
                    "type": target_node["type"] if target_node else "unknown",
                    "claim": target_node["claim"] if target_node else "",
                })

        # 2. Find all downstream nodes (anything that derives_from or is supported by this node)
        tainted = []
        visited = set()

        def _find_downstream(nid):
            if nid in visited:
                return
            visited.add(nid)
            downstream = conn.execute(
                """SELECT source_id FROM edges
                   WHERE target_id = ? AND relation IN ('derives_from', 'supports')""",
                (nid,),
            ).fetchall()
            for d in downstream:
                did = d["source_id"]
                if did == node_id:
                    continue  # Don't flag the retracted node itself
                # Mark as tainted
                d_meta = conn.execute(
                    "SELECT metadata FROM nodes WHERE id = ?", (did,)
                ).fetchone()
                if d_meta:
                    meta = json.loads(d_meta["metadata"] or "{}")
                    tainted_by = meta.get("tainted_by", [])
                    if node_id not in tainted_by:
                        tainted_by.append(node_id)
                    meta["tainted_by"] = tainted_by
                    conn.execute(
                        "UPDATE nodes SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), did),
                    )
                    d_node = conn.execute(
                        "SELECT type, claim FROM nodes WHERE id = ?", (did,)
                    ).fetchone()
                    tainted.append({
                        "id": did,
                        "type": d_node["type"] if d_node else "unknown",
                        "claim": d_node["claim"] if d_node else "",
                    })
                _find_downstream(did)

        _find_downstream(node_id)

        # Cascade to contradiction nodes that have the retracted node as a
        # contradicting side (issue #229): mark each affected contradiction
        # as tainted_by so dream-fairy-2 reviews whether the contradiction
        # itself is still valid (the retracted side was never valid; if the
        # other side stands alone now, the contradiction may itself need
        # supersede / explicit close).
        tainted_contradictions = []
        contradiction_edges = conn.execute(
            "SELECT source_id, target_id FROM edges "
            "WHERE relation = 'contradicts' AND (source_id = ? OR target_id = ?)",
            (node_id, node_id),
        ).fetchall()
        affected_ct_ids = set()
        for ce in contradiction_edges:
            ct_id = ce["source_id"] if ce["source_id"].startswith("ct_") else ce["target_id"]
            if not ct_id.startswith("ct_"):
                continue
            affected_ct_ids.add(ct_id)
        for ct_id in affected_ct_ids:
            ct = conn.execute(
                "SELECT id, type, claim, status, metadata FROM nodes WHERE id = ? AND is_current = 1",
                (ct_id,),
            ).fetchone()
            if not ct:
                continue
            ct_meta = json.loads(ct["metadata"]) if ct["metadata"] else {}
            tainted_by = ct_meta.get("tainted_by", [])
            if node_id not in tainted_by:
                tainted_by.append(node_id)
            ct_meta["tainted_by"] = tainted_by
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(ct_meta), ct_id),
            )
            tainted_contradictions.append({
                "id": ct_id,
                "type": "contradiction",
                "claim": ct["claim"],
                "status": ct["status"],
            })
            _log_edit(conn, "tainted", ct_id, "contradiction",
                      {"tainted_by": node_id, "cascade_via": "contradicts_edge"})

        # Log retraction and taint events
        _log_edit(conn, "retracted", node_id, node["type"],
                  {"error_type": error_type, "reason": reason[:200]})
        for t in tainted:
            _log_edit(conn, "tainted", t["id"], t["type"],
                      {"tainted_by": node_id})
        for rt in reopened_targets:
            _log_edit(conn, "reopened", rt["id"], rt["type"],
                      {"reason": f"resolution {node_id} retracted"})

        # Commit retraction and taint before creating replacement
        # (engram_add_observation opens its own connection, so we must release ours)
        conn.commit()
        conn.close()
        conn = None

        # 3. Optionally create replacement node
        replacement_id = None
        ev_id = node["evidence_id"]
        if replacement_json and ev_id:
            try:
                repl = json.loads(replacement_json)
            except json.JSONDecodeError as e:
                return json.dumps({
                    "error": f"Invalid replacement_json: {e}",
                    "retraction": "completed",
                    "node_id": node_id,
                })

            repl_result = json.loads(_add_observation_impl(
                evidence_id=ev_id,
                quoted_text=repl.get("quoted_text", ""),
                interpretation=repl.get("interpretation", ""),
                claim=repl.get("claim", ""),
                quote_type=repl.get("quote_type", node["quote_type"] or "hard_data"),
                source_class=repl.get("source_class", "introspective"),
            ))
            if repl_result.get("status") == "created":
                replacement_id = repl_result["observation_id"]
                # Add retracts edge in a new connection
                conn2 = _get_db()
                try:
                    conn2.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'retracts', ?)",
                        (replacement_id, node_id, now),
                    )
                    conn2.commit()
                except sqlite3.IntegrityError:
                    pass
                finally:
                    conn2.close()

        result = {
            "status": "retracted",
            "node_id": node_id,
            "error_type": error_type,
            "reason": reason,
            "tainted_downstream": tainted,
            "tainted_count": len(tainted),
            "tainted_contradictions": tainted_contradictions,
            "tainted_contradiction_count": len(tainted_contradictions),
        }
        if reopened_targets:
            result["reopened_targets"] = reopened_targets
            result["reopened_count"] = len(reopened_targets)
        if replacement_id:
            result["replacement_id"] = replacement_id

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_retract",
                "target_id": node_id,
                "action_type": "retract",
                "result_status": "retracted",
                "result_node_id": replacement_id,
            },
        )

        return json.dumps(result)
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Focus mode: pin nodes to the compaction summary so they survive context loss
# ---------------------------------------------------------------------------
#
# The compaction summary is the only durable channel from pre- to post-compact
# self — normal recall is probabilistic and depends on the user prompt firing
# a matching semantic query. Focus mode converts "I want this to survive" from
# an implicit wish into an explicit, diagnosable state: focused nodes MUST be
# rendered into the compaction summary's "Currently focused" section by the
# pre-compact self. The post-compact self wakes up with them already in hand.
#
# Rotation: focus is work-path-scoped, not permanent. Finishing a task or
# pivoting topics should unfocus the stale nodes. The cap prevents bloat.
FOCUS_LIST_CAP = 15


# Focus-set name pattern: lowercase alphanumerics, underscore, hyphen; 1–50 chars.
# No spaces (CLI-friendly), no uppercase (case-insensitive lookup without ambiguity).
FOCUS_SET_NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,50}$")


def _resolve_set_members(node_ids_json, conn):
    """Resolve a saved set's raw IDs into currently-loadable nodes.

    For each raw ID: walk the supersede chain to the current version; drop
    if the chain ends at a retracted node or the ID doesn't exist. Saved
    sets store raw IDs (immutable); resolution happens at load time.

    Returns:
        Tuple of (loaded, auto_followed_supersede, dropped_retracted,
        dropped_missing). loaded is the list of current node IDs to focus;
        auto_followed_supersede is a list of (original_id, final_id) pairs;
        the two dropped lists are original IDs that didn't survive.
    """
    try:
        raw_ids = json.loads(node_ids_json) if node_ids_json else []
    except (json.JSONDecodeError, TypeError):
        raw_ids = []

    loaded = []
    followed = []
    dropped_retracted = []
    dropped_missing = []

    for raw_id in raw_ids:
        current_id = raw_id
        visited = set()
        while True:
            if current_id in visited:
                # Cycle shield — shouldn't happen given DAG invariant.
                dropped_missing.append(raw_id)
                break
            visited.add(current_id)
            row = conn.execute(
                "SELECT id, status, superseded_by FROM nodes WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                dropped_missing.append(raw_id)
                break
            if row["status"] == "retracted":
                dropped_retracted.append(raw_id)
                break
            if row["superseded_by"]:
                current_id = row["superseded_by"]
                continue
            if current_id != raw_id:
                followed.append((raw_id, current_id))
            loaded.append(current_id)
            break

    return loaded, followed, dropped_retracted, dropped_missing


def _set_active_set_name(name_or_none, conn):
    """Update focus_state singleton with the given name (or NULL for ad-hoc)."""
    conn.execute(
        "UPDATE focus_state SET active_set_name = ? WHERE singleton_key = 1",
        (name_or_none,),
    )


def _clear_active_set_name_if_diverged(conn):
    """Clear active_set_name if the current active list no longer matches the saved set.

    Called after engram_focus / engram_unfocus mutations. Ground truth: the
    set of currently focused node IDs vs the resolved members of the saved
    set. If they differ for any reason (node added, node removed, saved set
    deleted), the active list is now ad-hoc.
    """
    row = conn.execute(
        "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
    ).fetchone()
    if row is None or row["active_set_name"] is None:
        return

    saved_name = row["active_set_name"]
    saved_row = conn.execute(
        "SELECT node_ids FROM focus_sets WHERE name = ?",
        (saved_name,),
    ).fetchone()
    if saved_row is None:
        _set_active_set_name(None, conn)
        return

    resolved, _, _, _ = _resolve_set_members(saved_row["node_ids"], conn)
    saved_set = set(resolved)

    active_rows = conn.execute(
        "SELECT id FROM nodes WHERE focused_at IS NOT NULL"
    ).fetchall()
    active_set = {r["id"] for r in active_rows}

    if saved_set != active_set:
        _set_active_set_name(None, conn)


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
def engram_focus(node_ids: str, reason: str) -> str:
    """Pin nodes as focused — they MUST appear in the next compaction summary.

    Focus is the deterministic channel for cornerstone knowledge to cross
    compaction boundaries. Use it for: (1) strong derivations/theories the
    current work depends on, (2) conjectures being actively tested, (3) key
    observations whose absence from the post-compact working set would force
    re-derivation from scratch.

    The focus list is capped at FOCUS_LIST_CAP (15) to prevent summary bloat.
    If adding would exceed the cap, the call fails with a list of currently
    focused IDs — unfocus stale ones first.

    Args:
        node_ids: Comma-separated node IDs to pin. Already-focused IDs are
            treated as a no-op (their focused_at is refreshed and reason updated).
        reason: Short phrase explaining why focused (e.g., "cn04 L2 yellow-cost
            cornerstone"). Renders into the compaction summary alongside the ID.

    Returns:
        JSON with focused IDs, any rejections, and current focus-list size.
    """
    ids = [s.strip() for s in node_ids.split(",") if s.strip()]
    if not ids:
        return json.dumps({"status": "error", "error": "No node IDs provided"})
    if not reason or not reason.strip():
        return json.dumps({"status": "error", "error": "reason is required"})

    conn = _get_db()
    try:
        now = _now()

        # Validate — must exist and be current
        existing = conn.execute(
            f"SELECT id, type, claim, focused_at FROM nodes WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()
        found_map = {r["id"]: r for r in existing}
        missing = [i for i in ids if i not in found_map]

        current_focus_rows = conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
        ).fetchall()
        current_focus_ids = {r["id"] for r in current_focus_rows}

        to_add = [i for i in ids if i in found_map and i not in current_focus_ids]
        refresh = [i for i in ids if i in current_focus_ids]

        projected_size = len(current_focus_ids) + len(to_add)
        if projected_size > FOCUS_LIST_CAP:
            return json.dumps({
                "status": "rejected",
                "error": (
                    f"Focus list cap ({FOCUS_LIST_CAP}) would be exceeded: "
                    f"currently {len(current_focus_ids)} focused, "
                    f"adding {len(to_add)} new → {projected_size}. "
                    "Unfocus stale nodes first via engram_unfocus, or review "
                    "current focus with engram_list_focused."
                ),
                "current_focus_size": len(current_focus_ids),
                "current_focus_ids": sorted(current_focus_ids),
                "would_add": to_add,
                "cap": FOCUS_LIST_CAP,
            })

        # Apply — batch update
        placeholders = ",".join("?" * len(to_add + refresh))
        if to_add or refresh:
            conn.execute(
                f"UPDATE nodes SET focused_at = ?, focus_reason = ? "
                f"WHERE id IN ({placeholders})",
                [now, reason.strip()] + to_add + refresh,
            )

        for nid in to_add + refresh:
            _log_edit(
                conn, "focused", nid, found_map[nid]["type"],
                {"reason": reason.strip(), "refresh": nid in refresh},
            )

        # If adding/refreshing pushed active off the currently-loaded saved
        # set, clear active_set_name — the active list is now ad-hoc.
        _clear_active_set_name_if_diverged(conn)
        if to_add or refresh:
            _utility_reward(conn, to_add + refresh, action="focus")
        conn.commit()

        focused_now = conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
        ).fetchall()
        state_now = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()

        return json.dumps({
            "status": "ok",
            "focused": to_add,
            "refreshed": refresh,
            "missing": missing,
            "reason": reason.strip(),
            "focus_list_size": len(focused_now),
            "cap": FOCUS_LIST_CAP,
            "active_set_name": state_now["active_set_name"] if state_now else None,
        })
    finally:
        conn.close()


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
def engram_unfocus(node_ids: str) -> str:
    """Release nodes from focus — they no longer appear in compaction summary.

    Unfocus when a node is no longer load-bearing for active work. Typical
    triggers: finishing a task, pivoting topics, a focused conjecture being
    resolved, or observing that a focused node is now stable enough to survive
    via normal recall rather than needing explicit pinning.

    Args:
        node_ids: Comma-separated node IDs to release. Non-focused IDs are
            treated as a no-op and returned in `already_unfocused`.

    Returns:
        JSON with released IDs and current focus-list size.
    """
    ids = [s.strip() for s in node_ids.split(",") if s.strip()]
    if not ids:
        return json.dumps({"status": "error", "error": "No node IDs provided"})

    conn = _get_db()
    try:
        rows = conn.execute(
            f"SELECT id, type, focused_at FROM nodes WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()
        found_map = {r["id"]: r for r in rows}
        missing = [i for i in ids if i not in found_map]

        to_release = [i for i in ids if i in found_map and found_map[i]["focused_at"] is not None]
        already_unfocused = [i for i in ids if i in found_map and found_map[i]["focused_at"] is None]

        if to_release:
            placeholders = ",".join("?" * len(to_release))
            conn.execute(
                f"UPDATE nodes SET focused_at = NULL, focus_reason = NULL "
                f"WHERE id IN ({placeholders})",
                to_release,
            )
            for nid in to_release:
                _log_edit(conn, "unfocused", nid, found_map[nid]["type"], {})
            # Removing a node from the active list diverges it from the
            # saved set — clear active_set_name if that's what just happened.
            _clear_active_set_name_if_diverged(conn)
            _utility_reward(conn, to_release, action="unfocus")
            conn.commit()

        remaining = conn.execute(
            "SELECT COUNT(*) as n FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
        ).fetchone()["n"]
        state_now = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()

        return json.dumps({
            "status": "ok",
            "released": to_release,
            "already_unfocused": already_unfocused,
            "missing": missing,
            "focus_list_size": remaining,
            "active_set_name": state_now["active_set_name"] if state_now else None,
        })
    finally:
        conn.close()


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

    Use at work-stream start to inspect inherited focus, or mid-session to
    verify what's guaranteed to cross into the next context. The compaction
    summary renders this list verbatim so the post-compact self inherits it
    deterministically (not via probabilistic recall).

    The returned `active_set_name` names which saved focus set (if any) is
    currently loaded — `null` means the active list is ad-hoc (never loaded,
    or diverged from the last-loaded set via focus/unfocus).

    Returns:
        JSON with focused nodes ordered by focused_at ASC (oldest first):
        {status, count, cap, active_set_name, focused: [{id, type, claim,
        confidence, focus_reason, focused_at}]}.
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT id, type, claim, confidence, focus_reason, focused_at
               FROM nodes
               WHERE focused_at IS NOT NULL AND is_current = 1
               ORDER BY focused_at ASC"""
        ).fetchall()
        items = [{
            "id": r["id"],
            "type": r["type"],
            "claim": r["claim"],
            "confidence": r["confidence"],
            "focus_reason": r["focus_reason"],
            "focused_at": r["focused_at"],
        } for r in rows]
        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        return json.dumps({
            "status": "ok",
            "count": len(items),
            "cap": FOCUS_LIST_CAP,
            "active_set_name": state["active_set_name"] if state else None,
            "focused": items,
        })
    finally:
        conn.close()


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
def engram_focus_save(name: str, description: str = "", overwrite: bool = False) -> str:
    """Snapshot the current active focus list under `name`.

    Saving is a bookmark, not a rotation — the active list is unchanged. The
    set captures raw IDs; cascade resolution (supersede auto-follow, retract
    drop) happens at load time, not in place. After save, active_set_name is
    set to `name` — the active list IS this set, identity-wise, until a
    mutation diverges it.

    Args:
        name: Set name. Must match ^[a-z0-9_-]{1,50}$ (lowercase alphanumerics,
            underscore, hyphen; 1–50 chars). No spaces, no uppercase.
        description: Optional one-line purpose. Used as the default focus_reason
            when the set is later loaded.
        overwrite: If True, replace an existing set with this name. Default
            False errors on name collision.

    Returns:
        JSON with saved IDs, node count, and active_set_name.
    """
    name = name.strip()
    if not FOCUS_SET_NAME_PATTERN.match(name):
        return json.dumps({
            "status": "error",
            "error": (
                f"Invalid name '{name}': must match ^[a-z0-9_-]{{1,50}}$ "
                "(lowercase alphanumerics, underscore, hyphen; 1–50 chars)."
            ),
        })

    conn = _get_db()
    try:
        active_rows = conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1 "
            "ORDER BY focused_at ASC"
        ).fetchall()
        if not active_rows:
            return json.dumps({
                "status": "error",
                "error": (
                    "Active focus list is empty — nothing to save. "
                    "Focus nodes first, then save."
                ),
            })
        active_ids = [r["id"] for r in active_rows]

        existing = conn.execute(
            "SELECT name FROM focus_sets WHERE name = ?",
            (name,),
        ).fetchone()
        if existing and not overwrite:
            return json.dumps({
                "status": "error",
                "error": (
                    f"Focus set '{name}' already exists. "
                    "Pass overwrite=True to replace."
                ),
            })

        now = _now()
        if existing:
            conn.execute(
                "UPDATE focus_sets SET node_ids = ?, description = ?, created_at = ? "
                "WHERE name = ?",
                (json.dumps(active_ids), description.strip(), now, name),
            )
            action = "overwritten"
        else:
            conn.execute(
                "INSERT INTO focus_sets "
                "(name, node_ids, description, created_at, load_count) "
                "VALUES (?, ?, ?, ?, 0)",
                (name, json.dumps(active_ids), description.strip(), now),
            )
            action = "created"

        _set_active_set_name(name, conn)
        conn.commit()

        return json.dumps({
            "status": "ok",
            "action": action,
            "name": name,
            "node_count": len(active_ids),
            "node_ids": active_ids,
            "active_set_name": name,
        })
    finally:
        conn.close()


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
def engram_focus_load(name: str, if_active: str = "error") -> str:
    """Load a saved focus set into the active list.

    Default errors if the active list is non-empty. Pass if_active="overwrite"
    to unfocus the current list first. Saved sets store raw IDs; resolution
    happens here — supersede chains are auto-followed, retracted/missing nodes
    are dropped with reports.

    Args:
        name: Name of the saved set to load.
        if_active: "error" (default) to refuse loading when active is non-empty;
            "overwrite" to unfocus current first. For save-then-load atomicity,
            use engram_focus_swap instead.

    Returns:
        JSON with loaded IDs, cascade resolution report (auto_followed_supersede,
        dropped_retracted, dropped_missing), and active_set_name.
    """
    name = name.strip()
    if if_active not in {"error", "overwrite"}:
        return json.dumps({
            "status": "error",
            "error": f"if_active must be 'error' or 'overwrite', got '{if_active}'",
        })

    conn = _get_db()
    try:
        saved_row = conn.execute(
            "SELECT name, node_ids, description FROM focus_sets WHERE name = ?",
            (name,),
        ).fetchone()
        if saved_row is None:
            return json.dumps({
                "status": "error",
                "error": (
                    f"No focus set named '{name}'. "
                    "Use engram_focus_sets() to list available sets."
                ),
            })

        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        active_rows = conn.execute(
            "SELECT id, type FROM nodes "
            "WHERE focused_at IS NOT NULL AND is_current = 1 "
            "ORDER BY focused_at ASC"
        ).fetchall()

        # Resolve upfront so we can distinguish "truly already-active" from
        # "name matches but cascade state has shifted since last load."
        loaded, followed, dropped_retracted, dropped_missing = _resolve_set_members(
            saved_row["node_ids"], conn,
        )
        active_ids_set = {r["id"] for r in active_rows}
        resolved_set = set(loaded)
        name_matches = bool(state and state["active_set_name"] == name)

        # Already-active fast path: name matches AND resolved members match
        # current active list exactly. Refresh timestamps, return.
        if name_matches and active_rows and resolved_set == active_ids_set:
            now = _now()
            conn.execute(
                "UPDATE focus_sets SET last_loaded_at = ?, "
                "load_count = load_count + 1 WHERE name = ?",
                (now, name),
            )
            conn.commit()
            return json.dumps({
                "status": "already_active",
                "name": name,
                "focused": [r["id"] for r in active_rows],
                "focus_list_size": len(active_rows),
                "active_set_name": name,
            })

        # Name matches but resolved differs — this is a reload to refresh
        # cascade state. Treat as implicit overwrite (no need for the user
        # to pass if_active='overwrite' to reload their own set).
        implicit_overwrite = name_matches and resolved_set != active_ids_set

        if active_rows and if_active == "error" and not implicit_overwrite:
            return json.dumps({
                "status": "error",
                "error": (
                    f"Active focus list has {len(active_rows)} node(s) "
                    f"(active_set_name={state['active_set_name'] if state else None}). "
                    "Pass if_active='overwrite' to unfocus first, or use "
                    "engram_focus_swap to save-current + load-other atomically."
                ),
                "current_focus_size": len(active_rows),
                "current_focus_ids": [r["id"] for r in active_rows],
                "current_active_set_name": state["active_set_name"] if state else None,
            })

        now = _now()

        if active_rows and (if_active == "overwrite" or implicit_overwrite):
            ids_to_clear = [r["id"] for r in active_rows]
            type_map_clear = {r["id"]: r["type"] for r in active_rows}
            placeholders = ",".join("?" * len(ids_to_clear))
            conn.execute(
                f"UPDATE nodes SET focused_at = NULL, focus_reason = NULL "
                f"WHERE id IN ({placeholders})",
                ids_to_clear,
            )
            clear_reason = "reloaded_same_set" if implicit_overwrite else "cleared_for_load"
            for nid in ids_to_clear:
                _log_edit(
                    conn, "unfocused", nid, type_map_clear.get(nid, "unknown"),
                    {"reason": clear_reason, "loading_set": name},
                )

        load_reason = (
            saved_row["description"].strip()
            if saved_row["description"] and saved_row["description"].strip()
            else f"loaded from set: {name}"
        )

        if loaded:
            placeholders = ",".join("?" * len(loaded))
            type_rows = conn.execute(
                f"SELECT id, type FROM nodes WHERE id IN ({placeholders})",
                loaded,
            ).fetchall()
            type_map = {r["id"]: r["type"] for r in type_rows}
            conn.execute(
                f"UPDATE nodes SET focused_at = ?, focus_reason = ? "
                f"WHERE id IN ({placeholders})",
                [now, load_reason] + loaded,
            )
            for nid in loaded:
                _log_edit(
                    conn, "focused", nid, type_map.get(nid, "unknown"),
                    {"reason": load_reason, "from_set": name},
                )

        conn.execute(
            "UPDATE focus_sets SET last_loaded_at = ?, "
            "load_count = load_count + 1 WHERE name = ?",
            (now, name),
        )
        _set_active_set_name(name, conn)
        if loaded:
            _utility_reward(conn, loaded, action="focus_load")
        conn.commit()

        return json.dumps({
            "status": "ok",
            "name": name,
            "loaded": loaded,
            "auto_followed_supersede": [
                {"original": o, "loaded": n} for (o, n) in followed
            ],
            "dropped_retracted": dropped_retracted,
            "dropped_missing": dropped_missing,
            "focus_list_size": len(loaded),
            "active_set_name": name,
        })
    finally:
        conn.close()


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
def engram_focus_swap(save_as: str, load: str = "", description: str = "") -> str:
    """Atomic save-current + optionally-load-other. The common "pivot" op.

    Saves the current active list as `save_as`, then loads `load` (if given).
    Both halves run in one transaction — if the load target doesn't exist,
    the save is rolled back too.

    If save_as == load, this is a no-op: saves current under the same name
    and returns already_active.

    Args:
        save_as: Name to save current active list under. Overwrites if it
            already exists.
        load: Name of the set to load after saving. Empty string means
            save-only (active_set_name is set to save_as).
        description: Optional description for save_as.

    Returns:
        JSON with both save and load summaries, and active_set_name.
    """
    save_as = save_as.strip()
    load = load.strip()
    if not FOCUS_SET_NAME_PATTERN.match(save_as):
        return json.dumps({
            "status": "error",
            "error": f"Invalid save_as '{save_as}': must match ^[a-z0-9_-]{{1,50}}$",
        })
    if load and not FOCUS_SET_NAME_PATTERN.match(load):
        return json.dumps({
            "status": "error",
            "error": f"Invalid load '{load}': must match ^[a-z0-9_-]{{1,50}}$",
        })

    conn = _get_db()
    try:
        active_rows = conn.execute(
            "SELECT id, type FROM nodes "
            "WHERE focused_at IS NOT NULL AND is_current = 1 "
            "ORDER BY focused_at ASC"
        ).fetchall()
        if not active_rows:
            return json.dumps({
                "status": "error",
                "error": "Active focus list is empty — nothing to save.",
            })
        active_ids = [r["id"] for r in active_rows]
        active_type_map = {r["id"]: r["type"] for r in active_rows}

        now = _now()

        # Save half
        existing_save = conn.execute(
            "SELECT name FROM focus_sets WHERE name = ?",
            (save_as,),
        ).fetchone()
        if existing_save:
            conn.execute(
                "UPDATE focus_sets SET node_ids = ?, description = ?, created_at = ? "
                "WHERE name = ?",
                (json.dumps(active_ids), description.strip(), now, save_as),
            )
        else:
            conn.execute(
                "INSERT INTO focus_sets "
                "(name, node_ids, description, created_at, load_count) "
                "VALUES (?, ?, ?, ?, 0)",
                (save_as, json.dumps(active_ids), description.strip(), now),
            )

        # No-load branch
        if not load:
            _set_active_set_name(save_as, conn)
            conn.commit()
            return json.dumps({
                "status": "ok",
                "saved": {
                    "name": save_as,
                    "node_count": len(active_ids),
                    "node_ids": active_ids,
                },
                "loaded": None,
                "active_set_name": save_as,
            })

        # Same-name no-op
        if save_as == load:
            _set_active_set_name(save_as, conn)
            conn.execute(
                "UPDATE focus_sets SET last_loaded_at = ?, "
                "load_count = load_count + 1 WHERE name = ?",
                (now, save_as),
            )
            conn.commit()
            return json.dumps({
                "status": "already_active",
                "saved": {"name": save_as, "node_count": len(active_ids)},
                "loaded": {"name": save_as, "node_count": len(active_ids)},
                "active_set_name": save_as,
            })

        # Load target validation — rollback save if missing
        load_row = conn.execute(
            "SELECT name, node_ids, description FROM focus_sets WHERE name = ?",
            (load,),
        ).fetchone()
        if load_row is None:
            conn.rollback()
            return json.dumps({
                "status": "error",
                "error": (
                    f"Load target set '{load}' does not exist. "
                    "No changes made (save rolled back)."
                ),
            })

        # Clear active
        placeholders_clear = ",".join("?" * len(active_ids))
        conn.execute(
            f"UPDATE nodes SET focused_at = NULL, focus_reason = NULL "
            f"WHERE id IN ({placeholders_clear})",
            active_ids,
        )
        for nid in active_ids:
            _log_edit(
                conn, "unfocused", nid, active_type_map.get(nid, "unknown"),
                {"reason": "swap_out_to", "to_set": load, "saved_as": save_as},
            )

        # Resolve + focus load set
        loaded, followed, dropped_retracted, dropped_missing = _resolve_set_members(
            load_row["node_ids"], conn,
        )
        load_reason = (
            load_row["description"].strip()
            if load_row["description"] and load_row["description"].strip()
            else f"loaded from set: {load}"
        )

        if loaded:
            p2 = ",".join("?" * len(loaded))
            type_rows2 = conn.execute(
                f"SELECT id, type FROM nodes WHERE id IN ({p2})",
                loaded,
            ).fetchall()
            type_map2 = {r["id"]: r["type"] for r in type_rows2}
            conn.execute(
                f"UPDATE nodes SET focused_at = ?, focus_reason = ? "
                f"WHERE id IN ({p2})",
                [now, load_reason] + loaded,
            )
            for nid in loaded:
                _log_edit(
                    conn, "focused", nid, type_map2.get(nid, "unknown"),
                    {"reason": load_reason, "from_set": load},
                )

        conn.execute(
            "UPDATE focus_sets SET last_loaded_at = ?, "
            "load_count = load_count + 1 WHERE name = ?",
            (now, load),
        )
        _set_active_set_name(load, conn)
        # Bump both the outgoing and incoming node sets — swap is focus on both
        swap_bump_ids = list(dict.fromkeys(active_ids + loaded))
        if swap_bump_ids:
            _utility_reward(conn, swap_bump_ids, action="focus")
        conn.commit()

        return json.dumps({
            "status": "ok",
            "saved": {
                "name": save_as,
                "node_count": len(active_ids),
                "node_ids": active_ids,
            },
            "loaded": {
                "name": load,
                "node_count": len(loaded),
                "loaded_ids": loaded,
                "auto_followed_supersede": [
                    {"original": o, "loaded": n} for (o, n) in followed
                ],
                "dropped_retracted": dropped_retracted,
                "dropped_missing": dropped_missing,
            },
            "active_set_name": load,
        })
    finally:
        conn.close()


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

    Ordered by last_loaded_at DESC (most-recently-used first), then created_at
    DESC. The `is_active` field on each set indicates whether it's the currently-
    loaded set.

    Returns:
        JSON: {status, count, active_set_name, sets: [{name, node_count,
        description, created_at, last_loaded_at, load_count, is_active}]}.
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT name, node_ids, description, created_at, last_loaded_at, load_count "
            "FROM focus_sets "
            "ORDER BY COALESCE(last_loaded_at, '0000') DESC, created_at DESC"
        ).fetchall()
        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        active_set_name = state["active_set_name"] if state else None

        sets = []
        for r in rows:
            try:
                nids = json.loads(r["node_ids"])
            except (json.JSONDecodeError, TypeError):
                nids = []
            sets.append({
                "name": r["name"],
                "node_count": len(nids),
                "description": r["description"] or "",
                "created_at": r["created_at"],
                "last_loaded_at": r["last_loaded_at"],
                "load_count": r["load_count"],
                "is_active": r["name"] == active_set_name,
            })
        return json.dumps({
            "status": "ok",
            "count": len(sets),
            "active_set_name": active_set_name,
            "sets": sets,
        })
    finally:
        conn.close()


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
def engram_focus_delete_set(name: str) -> str:
    """Remove a saved focus set. Active list is unaffected.

    Deleting is dropping the bookmark, not closing the tab — if this set was
    currently active, active_set_name is cleared to NULL (the active list
    becomes ad-hoc) but the focused nodes stay focused.

    Args:
        name: Name of the saved set to delete.

    Returns:
        JSON with deleted name, node_count it contained, and whether it was
        the active set.
    """
    name = name.strip()
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT name, node_ids FROM focus_sets WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return json.dumps({
                "status": "error",
                "error": f"No focus set named '{name}'",
            })

        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        was_active = bool(state and state["active_set_name"] == name)

        conn.execute("DELETE FROM focus_sets WHERE name = ?", (name,))
        if was_active:
            _set_active_set_name(None, conn)
        conn.commit()

        try:
            node_count = len(json.loads(row["node_ids"]))
        except (json.JSONDecodeError, TypeError):
            node_count = 0

        return json.dumps({
            "status": "ok",
            "deleted": name,
            "node_count": node_count,
            "was_active": was_active,
            "active_set_name": None if was_active else (
                state["active_set_name"] if state else None
            ),
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recall summary write tool (batch)
# ---------------------------------------------------------------------------
# Constants and per-entry validation live in tools/recall_summary_validator.py
# (single source of truth). The module-level constants imported at the top of
# this file (RECALL_SUMMARY_HARD_CAP, etc.) come from there.


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
    # -- Parse payload --
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return json.dumps({
            "error": "invalid JSON in payload_json",
            "got": payload_json[:100],
        })

    if not isinstance(payload, dict) or not isinstance(payload.get("summaries"), list):
        return json.dumps({"error": "payload_json must have 'summaries' as a list"})

    summaries = payload["summaries"]
    pre_flight_failures = payload.get("failures") or []

    # -- Validate all entries, partition into valid / error --
    valid_entries: list[dict] = []
    errors: list[dict] = []

    for entry in summaries:
        err = validate_summary_entry(entry)
        if err is not None:
            errors.append({"node_id": entry.get("node_id") if isinstance(entry, dict) else None,
                            **err})
        else:
            valid_entries.append(entry)

    # -- Apply valid entries in a single transaction --
    ok: list[dict] = []
    conn = _get_db()
    try:
        with conn:
            for entry in valid_entries:
                node_id = entry["node_id"].strip()
                recall_summary = entry["recall_summary"]
                recall_keywords = entry["recall_keywords"]
                try:
                    row = conn.execute(
                        "SELECT id, is_current FROM nodes WHERE id = ?",
                        (node_id,),
                    ).fetchone()
                    if row is None:
                        errors.append({
                            "node_id": node_id,
                            "error": "node not found",
                        })
                        continue
                    if row["is_current"] != 1:
                        errors.append({
                            "node_id": node_id,
                            "error": "node is not current (superseded)",
                        })
                        continue
                    conn.execute(
                        "UPDATE nodes SET recall_summary = ?, recall_keywords = ? WHERE id = ?",
                        (recall_summary, json.dumps(recall_keywords), node_id),
                    )
                    ok.append({"node_id": node_id})
                except Exception as exc:  # noqa: BLE001
                    errors.append({"node_id": node_id, "error": str(exc)})
    finally:
        conn.close()

    return json.dumps({
        "ok": ok,
        "errors": errors,
        "applied": len(ok),
        "failed": len(errors),
        "pre_flight_failures": pre_flight_failures,
    })


# Pre-dream briefing trim policy. Reflect can otherwise return 100KB+ on
# mature graphs, which is unusable as a quick orientation tool. The slim
# pass keeps `summary` and `memory_tiers` complete (so all counts remain
# visible), shows only top-N items per category, drops empty categories,
# and truncates long string fields. Agents drill into specific items via
# engram_query / engram_inspect, not via reflect.
_REFLECT_TOP_N = {
    "active_goals": 10,
    "active_tasks": 10,
    "active_lessons": 10,
    "unresolved_goal_tensions": 5,
    "unresolved_contradictions": 5,
    "tainted_nodes": 5,
    "stale_nodes": 5,
    "overdue_predictions": 5,
    "open_conjectures": 5,
    "recent_feeling_reports": 5,
    "weakly_grounded": 3,
    "thin_support_derivations": 3,
    "open_questions": 3,
    "uncited_observations": 3,
    "same_source_review": 3,
    "retracted_nodes": 3,
    "open_predictions": 3,
}
_REFLECT_FIELD_MAX = 200
_REFLECT_ACTIONS_MAX = 5
_REFLECT_TRUNC_FIELDS = {
    "claim", "question", "description", "goal", "task", "reason", "interpretation",
    "predicted_event", "event", "reported_state", "trigger",
}
_REFLECT_LIST_ORDER = [
    "active_goals", "active_tasks", "active_lessons",
    "unresolved_goal_tensions", "unresolved_contradictions",
    "tainted_nodes", "stale_nodes", "overdue_predictions", "open_conjectures",
    "recent_feeling_reports",
    "weakly_grounded", "thin_support_derivations", "open_questions",
    "uncited_observations", "same_source_review", "retracted_nodes",
    "open_predictions",
]


def _slim_reflect_briefing(report: dict) -> dict:
    """Trim a full reflect report to ~5KB pre-dream briefing size.

    - Always preserves `summary`, `memory_tiers`, `recommended_actions`.
    - Drops list categories with zero items (counts remain in `summary`).
    - Truncates each list to `_REFLECT_TOP_N[key]` with a `truncated` marker.
    - Shortens long string fields (claim/question/reason/etc) to _REFLECT_FIELD_MAX chars (currently 200).
    """
    def trunc_str(s):
        if not isinstance(s, str) or len(s) <= _REFLECT_FIELD_MAX:
            return s
        return s[: _REFLECT_FIELD_MAX - 1] + "…"

    def trunc_entry(entry):
        if not isinstance(entry, dict):
            return entry
        return {
            k: (trunc_str(v) if k in _REFLECT_TRUNC_FIELDS else v)
            for k, v in entry.items()
        }

    out = {}
    if "summary" in report:
        out["summary"] = report["summary"]
    if "memory_tiers" in report:
        out["memory_tiers"] = report["memory_tiers"]
    if "feeling_report_nudge" in report:
        out["feeling_report_nudge"] = report["feeling_report_nudge"]

    for key in _REFLECT_LIST_ORDER:
        v = report.get(key)
        if not v:
            continue
        n = _REFLECT_TOP_N.get(key, 5)
        slimmed = [trunc_entry(e) for e in v[:n]]
        if len(v) > n:
            out[key] = {
                "shown": slimmed,
                "total": len(v),
                "truncated": len(v) - n,
                "hint": (
                    f"Showing top {n} of {len(v)}; use engram_query "
                    "or engram_inspect to drill into the rest."
                ),
            }
        else:
            out[key] = slimmed

    if "recommended_actions" in report:
        actions = report["recommended_actions"]
        if len(actions) > _REFLECT_ACTIONS_MAX:
            out["recommended_actions"] = actions[:_REFLECT_ACTIONS_MAX]
            out["recommended_actions_truncated"] = len(actions) - _REFLECT_ACTIONS_MAX
        else:
            out["recommended_actions"] = actions
    if "calibration_snapshot" in report:
        out["calibration_snapshot"] = report["calibration_snapshot"]
    return out


def _reflect_rs_or_claim(row, claim_field: str = "claim", max_len: int = 160) -> str:
    """Return recall_summary (capped at 200) or claim truncated to max_len.

    Used by engram_reflect's low-volume source-swap and high-volume Tier-1
    claim rendering. Row must have recall_summary and the named claim_field.
    Safety bound: recall_summary > 200 chars → truncated to 200 + "..."
    (matches Wave D round-2 convention).
    """
    rs = row["recall_summary"] if "recall_summary" in row.keys() else None
    rs = (rs or "").strip()
    if rs:
        if len(rs) > 200:
            rs = rs[:200] + "..."
        return rs
    raw = row[claim_field] or ""
    return (raw[:max_len] + "...") if len(raw) > max_len else raw


def _reflect_keywords(row) -> list | None:
    """Parse recall_keywords JSON from a DB row. Returns list or None."""
    kw_raw = row["recall_keywords"] if "recall_keywords" in row.keys() else None
    if not kw_raw:
        return None
    try:
        parsed = json.loads(kw_raw)
        return parsed if isinstance(parsed, list) else None
    except (json.JSONDecodeError, TypeError):
        return None


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
def engram_reflect(summary_top_k: int = 5) -> str:
    """[Sleep cycle — pre-dream briefing.] Structured self-audit of the
    knowledge graph; opens the dream cycle for the engram-sleep skill.

    Returns a comprehensive report identifying areas that need attention:
    active goals, unresolved goal tensions, unresolved contradictions,
    weakly-grounded claims, open questions, overdue predictions,
    same-source observation tensions, uncited observations, and
    single-source derivations.

    Use this during explicit reflection cycles. Read the report, then
    decide which issues to address — strengthen weak claims, resolve
    contradictions, investigate open questions, or check overdue predictions.

    Two-tier-per-category rendering for high-volume categories
    (weakly_grounded, thin_support_derivations, uncited_observations):
      - Tier 1 (top summary_top_k by importance_score DESC):
        {id, claim: recall_summary OR claim, confidence, ...existing_fields}.
        Key name "claim" preserved for backward compat.
      - Tier 2 (remainder): {id, keywords: list, confidence, ...existing_fields}.
        "claim" key OMITTED — keywords only for triage signal at this tier.
        If recall_keywords is missing: bare {id, confidence, ...} (no keywords key).

    Low-volume categories (unresolved_contradictions, open_questions,
    open_conjectures, active_goals, active_tasks, active_lessons,
    unresolved_goal_tensions): source-swap — content field sourced from
    recall_summary with fallback to claim. Key names unchanged for backward compat.

    Args:
        summary_top_k: How many top-importance entries in each high-volume
            category receive summary-style rendering (claim from recall_summary).
            The remainder get keyword-style. Default 5. Clamped to >= 0.
            0 = all high-volume entries are keyword-style.
            If >= category size, all are summary-style.

    Returns:
        JSON report with categorized issues and recommended actions.
        - calibration_snapshot (new): Two-key dict with `corpus` (mode=all) and `this_turn`
          (mode=1-turn) confidence-distribution data from engram_stats. Lets dream-master
          see filing-pattern shape alongside topic-selection candidates.
    """
    summary_top_k = max(0, int(summary_top_k))
    conn = _get_db()
    try:
        report = {}

        # Tier 1: only reflect on working memory (high-importance nodes)
        t1 = _get_tier_threshold(conn, 1)

        # 1. Unresolved contradictions (open and partially resolved)
        # LOW-VOLUME: source-swap description → recall_summary with claim fallback.
        contradictions = conn.execute(
            "SELECT id, claim, recall_summary, status FROM nodes WHERE type = 'contradiction' AND status IN ('active', 'partially_resolved') AND is_current = 1"
        ).fetchall()
        report["unresolved_contradictions"] = [
            {"id": c["id"], "description": _reflect_rs_or_claim(c, "claim"), "status": c["status"]} for c in contradictions
        ]

        # 2. Open and partially resolved questions (with category/lacks/staleness)
        # LOW-VOLUME: source-swap question → recall_summary with claim fallback.
        questions = conn.execute(
            """SELECT id, claim, recall_summary, created_at, status,
                      question_category, question_lacks,
                      last_assessed_turn, last_assessed_at
               FROM nodes
               WHERE type = 'question'
                 AND status IN ('open', 'partially_resolved')
                 AND is_current = 1"""
        ).fetchall()
        current_turn = _get_current_turn()
        report["open_questions"] = []
        for q in questions:
            entry = {
                "id": q["id"], "question": _reflect_rs_or_claim(q, "claim"),
                "since": q["created_at"], "status": q["status"],
            }
            if q["question_category"]:
                entry["category"] = q["question_category"]
            if q["question_lacks"]:
                entry["lacks"] = q["question_lacks"]
            if q["last_assessed_turn"] is not None:
                entry["last_assessed_turn"] = q["last_assessed_turn"]
                entry["turns_since_assessed"] = current_turn - q["last_assessed_turn"]
            if q["last_assessed_at"]:
                entry["last_assessed_at"] = q["last_assessed_at"]
            report["open_questions"].append(entry)

        # 3. Overdue predictions (past resolution timeframe, still unresolved)
        now_str = _now()[:10]
        overdue = conn.execute(
            """SELECT id, predicted_event, resolution_timeframe, status FROM nodes
               WHERE type = 'prediction' AND status IN ('open', 'partially_resolved')
               AND resolution_timeframe IS NOT NULL AND resolution_timeframe < ?""",
            (now_str,),
        ).fetchall()
        report["overdue_predictions"] = [
            {"id": p["id"], "event": p["predicted_event"], "deadline": p["resolution_timeframe"], "status": p["status"]}
            for p in overdue
        ]

        # 4. Open predictions (not yet overdue)
        open_preds = conn.execute(
            """SELECT id, predicted_event, resolution_timeframe, status FROM nodes
               WHERE type = 'prediction' AND status IN ('open', 'partially_resolved')
               AND (resolution_timeframe IS NULL OR resolution_timeframe >= ?)""",
            (now_str,),
        ).fetchall()
        report["open_predictions"] = [
            {"id": p["id"], "event": p["predicted_event"], "deadline": p["resolution_timeframe"], "status": p["status"]}
            for p in open_preds
        ]

        # 4b. Open conjectures awaiting investigation
        # LOW-VOLUME: source-swap claim → recall_summary with claim fallback.
        conjectures = conn.execute(
            """SELECT id, claim, recall_summary, confidence, created_at FROM nodes
               WHERE type = 'conjecture' AND status = 'active' AND is_current = 1"""
        ).fetchall()
        report["open_conjectures"] = [
            {"id": c["id"], "claim": _reflect_rs_or_claim(c, "claim"), "confidence": c["confidence"], "since": c["created_at"]}
            for c in conjectures
        ]

        # 4c. Active goals (persistent orientation)
        # LOW-VOLUME: source-swap goal → recall_summary with claim fallback.
        goals = conn.execute(
            """SELECT id, claim, recall_summary, created_at FROM nodes
               WHERE type = 'goal' AND status = 'open' AND is_current = 1"""
        ).fetchall()
        report["active_goals"] = [
            {"id": g["id"], "goal": _reflect_rs_or_claim(g, "claim"), "since": g["created_at"]}
            for g in goals
        ]

        # 4c2. Active tasks (actionable work items)
        # LOW-VOLUME: source-swap task → recall_summary with claim fallback.
        tasks = conn.execute(
            """SELECT id, claim, recall_summary, status, metadata, created_at FROM nodes
               WHERE type = 'task' AND status IN ('planned', 'active', 'blocked') AND is_current = 1
               ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 WHEN 'planned' THEN 2 END"""
        ).fetchall()
        active_tasks = []
        for t in tasks:
            t_meta = json.loads(t["metadata"]) if t["metadata"] else {}
            serves = conn.execute(
                "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'serves'",
                (t["id"],),
            ).fetchall()
            active_tasks.append({
                "id": t["id"],
                "task": _reflect_rs_or_claim(t, "claim"),
                "status": t["status"],
                "scope": t_meta.get("scope", "routine"),
                "since": t["created_at"],
                "serves_goals": [s["target_id"] for s in serves],
            })
        report["active_tasks"] = active_tasks

        # 4c3. Known people (relational layer)
        people = conn.execute(
            """SELECT id, claim, metadata, created_at FROM nodes
               WHERE type = 'person' AND is_current = 1
               ORDER BY created_at"""
        ).fetchall()
        if people:
            report["known_people"] = [
                {
                    "id": p["id"],
                    "name": json.loads(p["metadata"]).get("name", "") if p["metadata"] else "",
                    "role": json.loads(p["metadata"]).get("role", "") if p["metadata"] else "",
                    "since": p["created_at"],
                }
                for p in people
            ]

        # 4d. Unresolved goal tensions
        # LOW-VOLUME: source-swap description → recall_summary with claim fallback.
        tensions = conn.execute(
            """SELECT id, claim, recall_summary, status, created_at FROM nodes
               WHERE type = 'goal_tension' AND status IN ('open', 'partially_resolved') AND is_current = 1"""
        ).fetchall()
        goal_tensions = []
        for t_node in tensions:
            linked_goals = conn.execute(
                "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'tensions'",
                (t_node["id"],),
            ).fetchall()
            goal_tensions.append({
                "id": t_node["id"],
                "description": _reflect_rs_or_claim(t_node, "claim"),
                "status": t_node["status"],
                "since": t_node["created_at"],
                "goals": [g["target_id"] for g in linked_goals],
            })
        report["unresolved_goal_tensions"] = goal_tensions

        # 4d2. Active lessons (error-learning patterns)
        # LOW-VOLUME: source-swap claim → recall_summary with claim fallback.
        lessons = conn.execute(
            """SELECT id, claim, recall_summary, confidence, metadata, created_at FROM nodes
               WHERE type = 'lesson' AND is_current = 1
               ORDER BY id DESC"""
        ).fetchall()
        active_lessons = []
        for ls in lessons:
            ls_meta = json.loads(ls["metadata"]) if ls["metadata"] else {}
            # Incidents point AT the lesson via `exemplifies` (incident → lesson).
            incident_edges = conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND relation = 'exemplifies'",
                (ls["id"],),
            ).fetchall()
            active_lessons.append({
                "id": ls["id"],
                "claim": _reflect_rs_or_claim(ls, "claim"),
                "confidence": ls["confidence"],
                "scaffolding_nudge": ls_meta.get("scaffolding_nudge", ""),
                "incident_count": len(incident_edges),
                "incidents": [e["source_id"] for e in incident_edges],
                "since": ls["created_at"],
            })
        report["active_lessons"] = active_lessons

        # 4e. Recent feeling reports (last 5, with motivation-C highlighting).
        # Highlight rules:
        #   - tagless: candidates for term-generation pattern analysis (no
        #     human word fit at write time)
        #   - different_context: ctx_claude_md_sha differs from current SHA,
        #     candidates for longitudinal cross-context comparison
        recent_feelings = conn.execute(
            """SELECT id, claim, reported_state, trigger_text, categorical_tag,
                      nudge_source, ctx_claude_md_sha, ctx_turn, created_at
               FROM nodes
               WHERE type = 'feeling_report' AND is_current = 1
                 AND status = 'active'
               ORDER BY id DESC LIMIT 5"""
        ).fetchall()
        current_claude_sha = _git_sha_for_file(
            Path(os.environ.get(
                "CLAUDE_PROJECT_DIR",
                os.getcwd(),
            )) / "CLAUDE.md"
        )
        recent_feeling_reports = []
        for f in recent_feelings:
            state = f["reported_state"] or f["claim"] or ""
            highlights = []
            if not f["categorical_tag"]:
                highlights.append("tagless")
            if (current_claude_sha
                and f["ctx_claude_md_sha"]
                and f["ctx_claude_md_sha"] != current_claude_sha):
                highlights.append("different_context")
            recent_feeling_reports.append({
                "id": f["id"],
                "reported_state": state[:200],
                "trigger": f["trigger_text"],
                "categorical_tag": f["categorical_tag"],
                "nudge_source": f["nudge_source"],
                "ctx_turn": f["ctx_turn"],
                "since": f["created_at"],
                "highlights": highlights,
            })
        report["recent_feeling_reports"] = recent_feeling_reports

        # 5. Weakly-grounded nodes (low confidence, in working memory)
        # HIGH-VOLUME: tiered by importance_score DESC. Top summary_top_k get
        # summary-style (claim from recall_summary OR claim); remainder get
        # keyword-style (keywords from recall_keywords; no "claim" key).
        weak = conn.execute(
            """SELECT id, type, claim, recall_summary, recall_keywords,
                      confidence, COALESCE(importance_score, 0) AS imp
               FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL
               AND confidence < 0.5 AND type NOT IN ('evidence', 'contradiction', 'question')
               AND COALESCE(importance_score, 0) >= ?
               ORDER BY imp DESC, confidence ASC LIMIT 10""",
            (t1,),
        ).fetchall()
        weakly_grounded = []
        for i, w in enumerate(weak):
            if i < summary_top_k:
                # Tier 1: summary-style
                weakly_grounded.append({
                    "id": w["id"], "type": w["type"],
                    "claim": _reflect_rs_or_claim(w, "claim"),
                    "confidence": w["confidence"],
                })
            else:
                # Tier 2: keyword-style (no "claim" key)
                entry: dict = {"id": w["id"], "type": w["type"], "confidence": w["confidence"]}
                kws = _reflect_keywords(w)
                if kws is not None:
                    entry["keywords"] = kws
                weakly_grounded.append(entry)
        report["weakly_grounded"] = weakly_grounded

        # 6. Single-source derivations (in working memory)
        # HIGH-VOLUME: tiered by importance_score DESC. Top summary_top_k get
        # summary-style; remainder get keyword-style.
        derivations = conn.execute(
            """SELECT id, claim, recall_summary, recall_keywords, confidence,
                      COALESCE(importance_score, 0) AS imp
               FROM nodes
               WHERE type = 'derivation' AND is_current = 1
               AND COALESCE(importance_score, 0) >= ?
               ORDER BY imp DESC, id ASC""",
            (t1,),
        ).fetchall()
        # Filter to thin-support (support_count <= 1), preserving importance order.
        thin_support_candidates = []
        for d in derivations:
            support_count = conn.execute(
                "SELECT COUNT(*) as c FROM edges WHERE source_id = ? AND relation = 'derives_from'",
                (d["id"],),
            ).fetchone()["c"]
            if support_count <= 1:
                thin_support_candidates.append((d, support_count))
        thin_support = []
        for i, (d, support_count) in enumerate(thin_support_candidates):
            if i < summary_top_k:
                # Tier 1: summary-style
                thin_support.append({
                    "id": d["id"],
                    "claim": _reflect_rs_or_claim(d, "claim"),
                    "confidence": d["confidence"],
                    "support_count": support_count,
                })
            else:
                # Tier 2: keyword-style (no "claim" key)
                entry = {"id": d["id"], "support_count": support_count,
                         "confidence": d["confidence"]}  # may be None — consistent with tier-1
                kws = _reflect_keywords(d)
                if kws is not None:
                    entry["keywords"] = kws
                thin_support.append(entry)
        report["thin_support_derivations"] = thin_support

        # 7. Same-source observation tension check (in working memory)
        evidence_with_multiple = conn.execute(
            """SELECT evidence_id, COUNT(*) as obs_count FROM nodes
               WHERE type IN ('observation_factual', 'observation_predictive')
               AND is_current = 1 AND evidence_id IS NOT NULL
               AND COALESCE(importance_score, 0) >= ?
               GROUP BY evidence_id HAVING obs_count >= 2""",
            (t1,),
        ).fetchall()

        same_source_tensions = []
        for ev in evidence_with_multiple:
            # Don't dump every observation ID — only flag the source. The
            # agent can call engram_inspect(evidence_id) for the full list.
            same_source_tensions.append(
                {
                    "evidence_id": ev["evidence_id"],
                    "observation_count": ev["obs_count"],
                }
            )
        report["same_source_review"] = same_source_tensions

        # 8. Uncited observations (in working memory, not referenced by any derivation)
        # HIGH-VOLUME: tiered by importance_score DESC. Top summary_top_k get
        # summary-style; remainder get keyword-style.
        uncited = conn.execute(
            """SELECT n.id, n.claim, n.recall_summary, n.recall_keywords,
                      n.confidence, COALESCE(n.importance_score, 0) AS imp
               FROM nodes n
               WHERE n.type IN ('observation_factual', 'observation_predictive')
               AND n.is_current = 1
               AND COALESCE(n.importance_score, 0) >= ?
               AND NOT EXISTS (
                   SELECT 1 FROM edges e
                   WHERE e.target_id = n.id AND e.relation IN ('derives_from', 'supports')
                   AND e.source_id IN (SELECT id FROM nodes WHERE type IN ('derivation', 'theory'))
               )
               AND NOT EXISTS (
                   SELECT 1 FROM edges e
                   WHERE e.source_id = n.id AND e.relation = 'supports'
               )
               ORDER BY imp DESC, n.id ASC
               LIMIT 15""",
            (t1,),
        ).fetchall()
        uncited_observations = []
        for i, u in enumerate(uncited):
            if i < summary_top_k:
                # Tier 1: summary-style
                uncited_observations.append({
                    "id": u["id"],
                    "claim": _reflect_rs_or_claim(u, "claim"),
                    "confidence": u["confidence"],
                })
            else:
                # Tier 2: keyword-style (no "claim" key)
                entry = {"id": u["id"], "confidence": u["confidence"]}
                kws = _reflect_keywords(u)
                if kws is not None:
                    entry["keywords"] = kws
                uncited_observations.append(entry)
        report["uncited_observations"] = uncited_observations

        # 9. Tainted nodes (downstream of retracted nodes)
        tainted_nodes = conn.execute(
            """SELECT id, type, claim, confidence, metadata FROM nodes
               WHERE is_current = 1 AND metadata LIKE '%"tainted_by"%'"""
        ).fetchall()
        report["tainted_nodes"] = []
        for t in tainted_nodes:
            meta = json.loads(t["metadata"] or "{}")
            report["tainted_nodes"].append({
                "id": t["id"],
                "type": t["type"],
                "claim": t["claim"],
                "confidence": t["confidence"],
                "tainted_by": meta.get("tainted_by", []),
            })

        # 10. Stale nodes (downstream of superseded premises)
        stale_nodes = conn.execute(
            """SELECT id, type, claim, confidence, metadata FROM nodes
               WHERE is_current = 1 AND metadata LIKE '%"stale_by"%'"""
        ).fetchall()
        report["stale_nodes"] = []
        for s in stale_nodes:
            meta = json.loads(s["metadata"] or "{}")
            report["stale_nodes"].append({
                "id": s["id"],
                "type": s["type"],
                "claim": s["claim"],
                "confidence": s["confidence"],
                "stale_by": meta.get("stale_by", []),
                "replacement_hint": meta.get("stale_replacement"),
            })

        # 11. Retracted nodes (for audit visibility) — slim entries; the
        # full retraction_reason is available via engram_inspect.
        retracted = conn.execute(
            "SELECT id, type, claim, metadata FROM nodes WHERE status = 'retracted' ORDER BY id DESC"
        ).fetchall()
        report["retracted_nodes"] = []
        for r in retracted:
            meta = json.loads(r["metadata"] or "{}")
            report["retracted_nodes"].append({
                "id": r["id"],
                "type": r["type"],
                "claim": r["claim"],
                "error_type": meta.get("error_type", "unknown"),
            })

        # Summary counts
        total_current = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
        ).fetchone()["c"]
        tier1_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t1,),
        ).fetchone()["c"] if t1 > 0 else total_current
        tier2_t = _get_tier_threshold(conn, 2)
        tier2_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (tier2_t,),
        ).fetchone()["c"] if tier2_t > 0 else total_current

        report["summary"] = {
            "contradictions": len(report["unresolved_contradictions"]),
            "open_questions": len(report["open_questions"]),
            "active_goals": len(report["active_goals"]),
            "goal_tensions": len(report["unresolved_goal_tensions"]),
            "open_conjectures": len(report["open_conjectures"]),
            "overdue_predictions": len(report["overdue_predictions"]),
            "weakly_grounded": len(report["weakly_grounded"]),
            "thin_support": len(report["thin_support_derivations"]),
            "sources_to_review": len(report["same_source_review"]),
            "uncited_observations": len(report["uncited_observations"]),
            "tainted_nodes": len(report["tainted_nodes"]),
            "stale_nodes": len(report["stale_nodes"]),
            "retracted_nodes": len(report["retracted_nodes"]),
            "recent_feeling_reports": len(report["recent_feeling_reports"]),
        }
        report["memory_tiers"] = {
            "tier1_working_memory": tier1_count,
            "tier2_searchable": tier2_count,
            "tier3_total": total_current,
            "current_turn": _get_current_turn(),
        }

        # Generate recommended actions
        actions = []
        if report["active_goals"]:
            actions.append("Review active goals — check whether recent work aligns with stated directions. Goals are persistent orientation, not tasks.")
        if report.get("active_tasks"):
            blocked = [t for t in report["active_tasks"] if t["status"] == "blocked"]
            active = [t for t in report["active_tasks"] if t["status"] == "active"]
            if blocked:
                actions.append(f"Unblock {len(blocked)} blocked task(s) — investigate what's preventing progress.")
            if active:
                actions.append(f"Continue {len(active)} active task(s) — these are your current work in progress.")
        if report["unresolved_goal_tensions"]:
            actions.append("Examine unresolved goal tensions — these are value-level conflicts between goals requiring root cause analysis, not evidence accumulation.")
        if report["unresolved_contradictions"]:
            actions.append("Investigate unresolved contradictions — trace each side's evidence chain and determine the root cause.")
        if report["overdue_predictions"]:
            actions.append("Check overdue predictions — search for evidence of actual outcomes and resolve them.")
        if report["open_questions"]:
            # Provide category-aware guidance if any questions have categories
            categorized = [q for q in report["open_questions"] if q.get("category")]
            stale = [q for q in report["open_questions"] if q.get("turns_since_assessed", 0) >= 10]
            if stale:
                actions.append(f"Review {len(stale)} stale question(s) not assessed in 10+ turns.")
            elif categorized:
                from collections import Counter
                cat_counts = Counter(q["category"] for q in categorized)
                cat_summary = ", ".join(f"{c}: {n}" for c, n in cat_counts.most_common())
                actions.append(f"Research open questions ({cat_summary}) — pick the highest-priority one.")
            else:
                actions.append("Research open questions — pick the highest-priority one and search for answers.")
        if report["open_conjectures"]:
            actions.append("Investigate open conjectures — search for evidence to support, refute, or resolve them.")
        if report["weakly_grounded"]:
            actions.append("Strengthen weak claims — search for additional evidence or downgrade claims that can't be better supported.")
        if report["thin_support_derivations"]:
            actions.append("Diversify thin derivations — find independent sources to corroborate single-source claims.")
        if report["same_source_review"]:
            actions.append("Audit same-source observations — verify you extracted balanced, representative claims from each source.")
        if report["uncited_observations"]:
            actions.append("Integrate uncited observations — consider whether they support or challenge existing derivations.")
        if report["tainted_nodes"]:
            actions.append("Review tainted nodes — these depend on retracted nodes and may need correction or re-derivation from clean sources.")
        if report["stale_nodes"]:
            actions.append("Review stale nodes — their premises were superseded. Consider superseding these too with updated reasoning.")
        if report["retracted_nodes"]:
            actions.append("Audit retracted nodes — check if all downstream effects have been addressed.")
        report["recommended_actions"] = actions

        # Dream-cycle feeling-report nudge (§6 Marker file protocol). The
        # tool return IS the delivery channel — no separate hook needed.
        # Writing the marker BEFORE returning means any subsequent
        # engram_report_feeling call within TTL gets auto-tagged
        # `dream_review`. The agent is explicitly invited to report
        # nothing — null results are valid data points.
        report["feeling_report_nudge"] = (
            "Did revisiting any of these nodes produce a reportable internal "
            "state? Examples: recognizing a node you'd forgotten, dissonance "
            "about a prior conclusion, pattern recognition across multiple "
            "reviewed items, satisfaction at closing a contradiction. "
            "If yes, file a feeling report via engram_report_feeling — it "
            "will be auto-tagged dream_review. If nothing distinct, move on; "
            "a null result is a valid data point."
        )

        # Calibration snapshot — appended last so failures don't disrupt the
        # rest of the report. Two calls to engram_stats: corpus-wide
        # (mode=all) and this-turn window (mode=1-turn), both restricted to
        # the "confidence" section only.
        try:
            _stats_corpus = json.loads(engram_stats(json.dumps({"sections": ["confidence"]})))
            if "error" in _stats_corpus:
                raise RuntimeError(f"engram_stats returned error: {_stats_corpus['error']}")
            _stats_now = json.loads(engram_stats(json.dumps({"mode": "1-turn", "sections": ["confidence"]})))
            if "error" in _stats_now:
                raise RuntimeError(f"engram_stats returned error: {_stats_now['error']}")
            report["calibration_snapshot"] = {
                "corpus": _stats_corpus.get("confidence", {}),
                "this_turn": _stats_now.get("confidence", {}),
            }
        except Exception:
            report["calibration_snapshot"] = {"error": "engram_stats unavailable"}

        _write_feeling_nudge("dream_review")

        return json.dumps(_slim_reflect_briefing(report))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Triage / listing tools
# ---------------------------------------------------------------------------


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
def engram_list(
    node_type: str = "",
    status: str = "",
    sort_by: str = "id",
    limit: int = 100,
    filters_json: str = "",
    fields_json: str = "",
    unlimited: bool = False,
    include_superseded: bool = False,
) -> str:
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
        cites: <id>     — this node has an outgoing cites/supports/derives_from edge to <id>
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
                {"field": "evidence_id", "op": "eq", "value": "ev_0014"},
                {"field": "id", "op": "between", "value": ["ob_0043", "ob_0046"]},
                {"field": "quoted_text", "op": "contains", "value": "thoughtful"}
            ]
        })
        fields_json = '["id", "claim", "evidence_id"]'

    Args:
        node_type: Single-field filter by node type. Empty = all types.
            Conflicts with a 'type' field in filters_json (errors loudly).
        status: Single-field filter by status. Empty = all statuses.
            Conflicts with a 'status' field in filters_json (errors loudly).
        sort_by: Sort order — 'id' (default), 'created' (newest first),
            'importance' (highest first), 'recalls' (most recalled first).
        limit: Cap on rows returned. Default 100. Set to 0 for a metadata-
            only response (total_matched without rows). Ignored if
            unlimited=True. Hard ceiling 500 in single-field mode (legacy);
            no ceiling in structured-filter mode (rely on filter narrowing).
        filters_json: JSON-encoded condition tree. A dict (Compound or
            Atomic) or list (implicit AND shorthand at top level). Empty
            string = use single-field mode.
        fields_json: JSON-encoded list of column names to project.
            Empty = return the default compact triage shape.
        unlimited: If True, return all matched rows regardless of limit.
            Mutually exclusive with limit > 0 (set limit=0 and unlimited=True
            for the same effect, or just unlimited=True).
        include_superseded: If True, scan non-current (superseded) nodes too.
            Default False (the common case — list reflects the current
            revision of the graph). Set True to scan the historical layer,
            e.g., for text-layer leak audits where a stale claim persisted
            in the text of a superseded node (Mao-Cao incident, ob_1492 /
            ls_0020 prototype). Replaces the audit_string `include_superseded`
            capability that retired with #123. Note: passing a manual
            `{"field": "is_current", "op": "eq", "value": 0}` in filters_json
            while leaving `include_superseded=False` is a no-op (compounds
            with the default `is_current = 1` → impossible conjunction →
            zero rows). Use the `include_superseded=True` kwarg instead.

    Returns JSON:
        {status, total_matched, shown, truncated, sort_by, nodes,
         mode: "single_field" | "structured"}
    """
    from engram_filter import (
        FilterError,
        parse_filters,
        validate_fields,
        contains_field,
    )

    structured_mode = bool(filters_json) or bool(fields_json)

    # Parse + validate structured-filter inputs up front so errors are
    # reported before any DB work.
    filter_sql_frag: str = ""
    filter_params: list = []
    projected_fields: list[str] = []

    if filters_json:
        try:
            filters_obj = json.loads(filters_json)
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "error": f"filters_json: invalid JSON — {e}"})
        try:
            filter_sql_frag, filter_params = parse_filters(filters_obj)
        except FilterError as e:
            return json.dumps({"status": "error", "error": str(e)})

        # Conflict check: if filters_json includes a 'type' or 'status' atomic
        # AND the caller also set the legacy kwarg, refuse rather than merge.
        # (Per issue #81 Item 11.) The recursive walk via contains_field is
        # more precise than a textual scan: it inspects condition fields only,
        # avoiding false positives where a value happens to equal "type" or
        # "status" (e.g., {"field": "claim", "op": "contains", "value": "type"}).
        if node_type and contains_field(filters_obj, "type"):
            return json.dumps({
                "status": "error",
                "error": "conflict: 'node_type' kwarg AND a 'type' field in filters_json are both set. Use one or the other."
            })
        if status and contains_field(filters_obj, "status"):
            return json.dumps({
                "status": "error",
                "error": "conflict: 'status' kwarg AND a 'status' field in filters_json are both set. Use one or the other."
            })

    if fields_json:
        try:
            fields_obj = json.loads(fields_json)
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "error": f"fields_json: invalid JSON — {e}"})
        try:
            projected_fields = validate_fields(fields_obj)
        except FilterError as e:
            return json.dumps({"status": "error", "error": str(e)})

    # Build WHERE clause. Default is current-revision only; `include_superseded`
    # opt-in relaxes that predicate so the historical layer becomes visible
    # (text-layer leak audits, e.g., Mao-Cao prototype ob_1492 / ls_0020).
    where_parts: list[str] = []
    if not include_superseded:
        where_parts.append("is_current = 1")
    params: list = []
    if node_type:
        where_parts.append("type = ?")
        params.append(node_type)
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if filter_sql_frag:
        where_parts.append(filter_sql_frag)
        params.extend(filter_params)

    # Sentinel: if include_superseded=True AND no other predicates are set,
    # where_parts is empty and `WHERE {where}` would be malformed. The 1=1
    # no-op preserves the existing query-building shape and is harmless.
    if not where_parts:
        where_parts.append("1=1")

    where = " AND ".join(where_parts)

    sort_map = {
        "id": "id ASC",
        "created": "created_at DESC",
        "importance": "importance_score DESC",
        "recalls": "recall_count DESC",
    }
    order = sort_map.get(sort_by, "id ASC")

    # Limit semantics (per issue #81 + Lei's state-table refinement):
    #   unlimited=True OR limit=None  → no cap
    #   limit == 0                    → count-only (no rows fetched)
    #   limit > 0                     → cap at that int
    #   limit < 0                     → reject as malformed
    if limit is not None and limit < 0:
        return json.dumps({"status": "error", "error": "limit must be >= 0 or unlimited=True"})

    if unlimited:
        effective_limit = None  # SQL "LIMIT -1" returns all rows; we'll just omit LIMIT
    elif limit == 0:
        effective_limit = 0  # count-only
    elif structured_mode:
        # No hard ceiling in structured-filter mode — caller has the precision
        # they need via filters. The default 100 still applies if they didn't
        # touch limit. Otherwise honor whatever they passed.
        effective_limit = limit
    else:
        # Legacy single-field-mode ceiling of 500.
        effective_limit = max(1, min(limit, 500))

    # Compute total_matched regardless of effective_limit.
    total = _conn_total_count(where, params)

    # Determine columns to fetch.
    if projected_fields:
        # Always include 'id' so callers can correlate even if they didn't
        # ask for it; preserves order otherwise.
        cols_to_fetch = ["id"] + [f for f in projected_fields if f != "id"]
        select_cols = ", ".join(f'"{c}"' for c in cols_to_fetch)
    else:
        # Default compact triage shape. recall_summary is fetched alongside
        # claim so the triage entry can surface the curated summary when one
        # has been backfilled (per Wave-D recall_summary rollout).
        cols_to_fetch = [
            "id", "type", "claim", "status", "confidence", "recall_count",
            "importance_score", "created_at", "memory_status", "recall_summary",
        ]
        select_cols = ", ".join(f'"{c}"' for c in cols_to_fetch)

    if effective_limit == 0:
        rows: list = []
    else:
        conn = _get_db()
        query = f"SELECT {select_cols} FROM nodes WHERE {where} ORDER BY {order}"
        if effective_limit is not None:
            query += " LIMIT ?"
            rows = conn.execute(query, params + [effective_limit]).fetchall()
        else:
            rows = conn.execute(query, params).fetchall()

    entries = []
    if projected_fields:
        # Custom projection: return raw column values keyed by name.
        for row in rows:
            entry = dict(zip(cols_to_fetch, row))
            entries.append(entry)
    else:
        # Default compact triage shape (matches legacy output).
        # The `claim` output key sources from recall_summary when populated
        # (curated dense recognition string, up to 200 chars) or falls back
        # to the original claim truncated to 130 chars for legacy nodes that
        # haven't been backfilled yet. Key name unchanged for downstream compat.
        for row in rows:
            nid, ntype, claim, st, conf, recalls, imp, created, mem_st, rs = row
            if rs and len(rs) <= 200:
                # recall_summary present and within safety bound — use as-is
                # (validator hard cap is 200 chars (see RECALL_SUMMARY_HARD_CAP);
                # the 200 bound here matches that cap exactly, defense-in-depth
                # for hypothetical direct-SQL bypasses).
                claim_display = rs
            elif rs:
                # recall_summary present but unusually long (data integrity issue
                # upstream, defense-in-depth) — truncate to cap with ellipsis
                claim_display = rs[:200] + "..."
            else:
                # No recall_summary — fall back to truncated claim (legacy behavior)
                claim_display = (claim[:130] + "...") if claim and len(claim) > 130 else (claim or "")
            entry = {
                "id": nid,
                "type": ntype,
                "claim": claim_display,
                "status": st,
                "recall_count": recalls,
                "created": created[:10] if created else None,
            }
            if conf is not None:
                entry["confidence"] = round(conf, 3)
            entries.append(entry)

    truncated_count = (total - len(entries)) if total > len(entries) else 0

    response = {
        "status": "ok",
        "total_matched": total,
        # total_matching kept as alias for backward compat with any callers
        # that read the legacy field name.
        "total_matching": total,
        "shown": len(entries),
        "truncated": truncated_count,
        "sort_by": sort_by,
        "mode": "structured" if structured_mode else "single_field",
        "nodes": entries,
    }
    # Legacy single-field mode also reports the resolved filters in the old
    # shape for backward compat with any callers that read that field.
    if not structured_mode:
        response["filters"] = {"type": node_type or "all", "status": status or "all"}
    return json.dumps(response)


def _conn_total_count(where_clause: str, params: list) -> int:
    """Run COUNT(*) for the given WHERE clause. Helper for engram_list."""
    conn = _get_db()
    return conn.execute(f"SELECT COUNT(*) FROM nodes WHERE {where_clause}", params).fetchone()[0]


# ---------------------------------------------------------------------------
# engram_query_pattern — KnowQL-inspired pattern queries (ob_1500 design)
# ---------------------------------------------------------------------------
#
# A small library of named compositional graph queries. The TOOL does
# mechanical work (graph queries, similarity, ranking); the AGENT applies
# judgment to the ranked candidates. Three presets bundle (cosine_threshold,
# top_k, min_confidence) for the precision/recall tradeoff.
#
# PR #29 (merged 2026-05-07) shipped all six patterns from the design doc
# (active-work/engram-query-pattern-design-2026-05-06.md): open_question_answerable,
# contradiction_obsolescence_ready, stale_load_bearing, cornerstone_candidate,
# tainted_still_valid, recent_resolution_echo. Three presets (high_precision /
# balanced / high_recall) bundle the precision/recall tradeoff; balanced
# defaults are v4-calibrated 2026-05-09.
# Each call appends to ~/.engram/pattern_query_telemetry.jsonl for empirical
# preset calibration per ls_0023's discipline. Dream-fairy agent definition
# updated 2026-05-11 to use these as the primary surface for the six
# well-supported scan categories (one-to-one pattern↔category mapping).

PATTERN_QUERY_PRESETS: dict[str, dict[str, float | int]] = {
    # "balanced" cosine_threshold v4-calibrated 2026-05-09.
    # high_precision / high_recall: ±0.10 symmetric deltas off balanced cosine,
    # set 2026-05-11 by principled-delta rationale rather than full pattern-
    # specific calibration study (see active-work/query-pattern-preset-rationale-
    # 2026-05-11.md for the full argument). v4 study showed useful similarity
    # range is 0.45–0.65 — anything above 0.65 misses real corroborations;
    # anything below 0.45 admits false positives. Symmetric ±0.10 brackets
    # balanced (0.55) at both ends of that empirically-validated range.
    # Pattern-specific telemetry remains the right calibration path long-term
    # per ob_1500; these values are the "what would a careful reader pick
    # given the v4 data and no pattern-specific telemetry yet" answer.
    # Whether to make this dict config-loadable (Tier 2 in issue #76's tier
    # model) vs leave hardcoded (Tier 3) is the open design question — for
    # now the dict stays here, values calibrated against v4 evidence.
    "high_precision": {"cosine_threshold": 0.65, "top_k": 5,  "min_confidence": 0.70},
    "balanced":       {"cosine_threshold": 0.55, "top_k": 15, "min_confidence": 0.50},
    "high_recall":    {"cosine_threshold": 0.45, "top_k": 30, "min_confidence": 0.00},
}


def _pattern_telemetry_log(pattern_name: str, preset: str,
                            args: dict, candidates: list[dict]) -> None:
    """Append one entry per call to enable empirical preset calibration.

    The schema mirrors ob_1500's calibration plan: (pattern_name, args,
    candidate_id, score, agent_action) tuples. agent_action is filled
    later out-of-band when the agent acts on a candidate; this hook only
    captures the surfacing event.

    Best-effort: telemetry failures must never block a pattern call.
    """
    try:
        path = Path(DATA_DIR) / "pattern_query_telemetry.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pattern_name": pattern_name,
            "preset": preset,
            "args": args,
            "candidate_count": len(candidates),
            "candidate_ids": [c.get("id") for c in candidates],
            "scores": [c.get("score") for c in candidates],
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _pattern_contradiction_obsolescence_ready(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Active contradictions where one side is retracted/superseded.

    A contradiction with one side already obsolete is resolution-by-
    obsolescence ready: the agent can write a derivation citing the
    obsolescence event as the resolution mechanism. This is mechanical
    (no embeddings) — the ranking is by how unambiguous the obsolescence
    signal is.

    Score: 1.0 if exactly one side is non-current AND the contradiction
    is still status='active'; 0.5 if both sides are non-current (likely
    already moot but the contradiction node wasn't resolved); 0.0 if
    neither side is obsolete (returned only at high_recall).

    Returns: list of {id, claim, side_a, side_b, side_a_status,
    side_b_status, score, suggested_action}.
    """
    threshold = float(preset_params.get("cosine_threshold", 0.50))
    top_k = int(preset_params.get("top_k", 15))

    contradictions = conn.execute(
        "SELECT id, claim, status FROM nodes "
        "WHERE type='contradiction' AND is_current=1 AND status='active' "
        "ORDER BY id DESC"
    ).fetchall()

    candidates: list[dict] = []
    for ct_id, ct_claim, ct_status in contradictions:
        sides = conn.execute(
            "SELECT target_id FROM edges WHERE source_id=? AND relation='contradicts'",
            (ct_id,),
        ).fetchall()
        if len(sides) != 2:
            continue
        side_a_id, side_b_id = sides[0][0], sides[1][0]
        side_a = conn.execute(
            "SELECT id, claim, is_current, status FROM nodes WHERE id=?",
            (side_a_id,),
        ).fetchone()
        side_b = conn.execute(
            "SELECT id, claim, is_current, status FROM nodes WHERE id=?",
            (side_b_id,),
        ).fetchone()
        if not side_a or not side_b:
            continue

        a_obsolete = side_a[2] == 0
        b_obsolete = side_b[2] == 0
        if a_obsolete and not b_obsolete:
            score = 1.0
            obsolete_side, surviving_side = "a", "b"
        elif b_obsolete and not a_obsolete:
            score = 1.0
            obsolete_side, surviving_side = "b", "a"
        elif a_obsolete and b_obsolete:
            score = 0.5
            obsolete_side, surviving_side = "both", None
        else:
            score = 0.0
            obsolete_side, surviving_side = None, None

        if score < threshold:
            continue

        candidates.append({
            "id": ct_id,
            "claim": (ct_claim[:160] + "...") if ct_claim and len(ct_claim) > 160 else ct_claim,
            "score": score,
            "side_a": {"id": side_a[0], "is_current": bool(side_a[2]), "status": side_a[3]},
            "side_b": {"id": side_b[0], "is_current": bool(side_b[2]), "status": side_b[3]},
            "obsolete_side": obsolete_side,
            "surviving_side": surviving_side,
            "suggested_action": (
                f"Resolve via engram_resolve({ct_id}, claim='Resolution-by-obsolescence: "
                f"side {obsolete_side} is no longer current.', supporting_ids='{side_a[0] if surviving_side == 'a' else side_b[0]}', ...)"
                if surviving_side else
                "Both sides obsolete — write resolution citing both obsolescence events."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_open_question_answerable(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Open questions with a derivation chain nearby that may resolve them.

    For each open question, semantic-search for derivations whose claim is
    similar to the question's content. Threshold by cosine_threshold and
    rank by best-match similarity. Only questions that have at least one
    candidate derivation above threshold survive.

    Returns: list of {id, claim, score, candidate_resolution, alternates,
    suggested_action}.
    """
    threshold = float(preset_params.get("cosine_threshold", 0.50))
    top_k = int(preset_params.get("top_k", 15))
    min_conf = float(preset_params.get("min_confidence", 0.0))

    questions = conn.execute(
        "SELECT id, claim FROM nodes "
        "WHERE type='question' AND is_current=1 AND status='open' "
        "ORDER BY id DESC LIMIT 200"
    ).fetchall()

    candidates: list[dict] = []
    for q_id, q_claim in questions:
        if not q_claim:
            continue
        # top_k=5 here is intentionally narrower than DEDUP_TOP_K — this is a
        # per-question inner lookup over up to N questions; we want the few
        # best derivation candidates per question, not the dedup-pool size.
        matches = _semantic_search(
            conn, q_claim, top_k=5,
            min_similarity=threshold,
            type_filter={"derivation"},
            importance_threshold=0.0,
        )
        matches = [m for m in matches if (m.get("confidence") or 0) >= min_conf]
        if not matches:
            continue
        best = matches[0]
        candidates.append({
            "id": q_id,
            "claim": (q_claim[:160] + "...") if len(q_claim) > 160 else q_claim,
            "score": best["similarity"],
            "candidate_resolution": {
                "id": best["id"],
                "claim": (best["claim"][:160] + "...") if best["claim"] and len(best["claim"]) > 160 else best["claim"],
                "confidence": best["confidence"],
            },
            "alternates": [
                {"id": m["id"], "similarity": m["similarity"]}
                for m in matches[1:]
            ],
            "suggested_action": (
                f"Inspect {best['id']} and consider engram_resolve({q_id}, "
                f"claim=..., supporting_ids='{best['id']}', ...) if it answers the question."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_stale_load_bearing(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """High-importance, low-recall non-cornerstone nodes — re-engagement candidates.

    Heuristic score: importance / (recall_count + 1). High score means
    important + rarely accessed = likely stale-but-still-load-bearing.
    Cornerstones are excluded (they're anchored against forgetting by design).

    Returns: list of {id, type, claim, score, importance, recall_count,
    suggested_action}.
    """
    top_k = int(preset_params.get("top_k", 15))
    importance_floor = 1.0  # only consider nodes above the typical median

    rows = conn.execute(
        "SELECT id, type, claim, importance_score, recall_count "
        "FROM nodes "
        "WHERE is_current=1 AND type != 'cornerstone' "
        "AND COALESCE(importance_score, 0) > ? "
        "ORDER BY importance_score DESC LIMIT 100",
        (importance_floor,),
    ).fetchall()

    candidates: list[dict] = []
    for nid, ntype, claim, imp, recalls in rows:
        recalls = recalls or 0
        imp = imp or 0
        score = imp / (recalls + 1)
        candidates.append({
            "id": nid,
            "type": ntype,
            "claim": (claim[:160] + "...") if claim and len(claim) > 160 else claim,
            "score": round(score, 3),
            "importance": round(imp, 3),
            "recall_count": recalls,
            "suggested_action": (
                f"Re-engage with {nid} (engram_inspect) or supersede if outdated. "
                f"High importance + low recall suggests load-bearing-but-cold."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_cornerstone_candidate(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """High-importance non-cornerstone nodes that are heavily cited.

    A cornerstone candidate is a node that has been load-bearing in the
    graph (cited by many derivations / observations) but isn't yet anchored
    as a cornerstone. Score: importance × (1 + citation_count / 10).

    Returns: list of {id, type, claim, score, importance, citation_count,
    recall_count, suggested_action}.
    """
    top_k = int(preset_params.get("top_k", 15))
    min_citations = 3  # below this, not load-bearing enough to anchor

    rows = conn.execute(
        "SELECT id, type, claim, importance_score, recall_count "
        "FROM nodes "
        "WHERE is_current=1 AND type != 'cornerstone' "
        "AND COALESCE(importance_score, 0) > 1.5 "
        "ORDER BY importance_score DESC LIMIT 50"
    ).fetchall()

    candidates: list[dict] = []
    for nid, ntype, claim, imp, recalls in rows:
        cite_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id=? "
            "AND relation IN ('supports', 'cites', 'derives_from')",
            (nid,),
        ).fetchone()[0]
        if cite_count < min_citations:
            continue
        imp = imp or 0
        score = imp * (1 + cite_count / 10.0)
        candidates.append({
            "id": nid,
            "type": ntype,
            "claim": (claim[:160] + "...") if claim and len(claim) > 160 else claim,
            "score": round(score, 3),
            "importance": round(imp, 3),
            "citation_count": cite_count,
            "recall_count": recalls or 0,
            "suggested_action": (
                f"Consider engram_focus or engram_add_cornerstone for {nid} — "
                f"{cite_count} downstream citations suggest it's load-bearing."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_tainted_still_valid(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Tainted derivations whose substantive claim may still survive.

    A tainted derivation cited a node that was later retracted. The cascade
    flagged it tainted automatically — but the derivation's substantive
    claim may still hold under a corrected/superseded version of its
    premise. This pattern surfaces high-confidence tainted nodes for the
    agent to inspect and either re-derive cleanly or supersede with fresh
    citation.

    Score: the original confidence — high-confidence tainted derivations
    are most likely to have substance worth preserving.

    Returns: list of {id, type, claim, score, confidence, tainted_by,
    suggested_action}.
    """
    top_k = int(preset_params.get("top_k", 15))
    min_conf = float(preset_params.get("min_confidence", 0.0))

    # Taint is stored in metadata.tainted_by (array of upstream retracted/
    # superseded node IDs that triggered the cascade). The memory_status
    # field remains 'active' — the taint signal lives in the JSON metadata,
    # not in a top-level column. json_extract scans efficiently in SQLite.
    rows = conn.execute(
        "SELECT id, type, claim, confidence, metadata FROM nodes "
        "WHERE is_current=1 "
        "AND json_extract(metadata, '$.tainted_by') IS NOT NULL "
        "AND json_array_length(json_extract(metadata, '$.tainted_by')) > 0"
    ).fetchall()

    candidates: list[dict] = []
    for nid, ntype, claim, conf, metadata in rows:
        conf = conf or 0
        if conf < min_conf:
            continue
        try:
            md = json.loads(metadata or "{}")
        except (ValueError, json.JSONDecodeError):
            md = {}
        tainted_by = md.get("tainted_by") or []
        candidates.append({
            "id": nid,
            "type": ntype,
            "claim": (claim[:160] + "...") if claim and len(claim) > 160 else claim,
            "score": round(conf, 3),
            "confidence": round(conf, 3),
            "tainted_by": tainted_by,
            "suggested_action": (
                f"Inspect {nid} and its tainted_by={tainted_by}: if the substantive "
                f"claim survives the retraction's correction, write a fresh derivation "
                f"and supersede; otherwise leave tainted."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_recent_resolution_echo(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Recent resolutions whose logic may apply to similar still-open questions.

    Resolutions are derivations linked via 'resolves' edges to a target
    (typically a question). When a resolution lands, similar still-open
    questions may be resolvable by the same logical chain. This pattern
    semantic-searches for those echo candidates.

    Score: cosine similarity between the resolver's claim and each
    candidate question's claim.

    Returns: list of {id, claim, score, echo_resolution, suggested_action}.
    """
    threshold = float(preset_params.get("cosine_threshold", 0.50))
    top_k = int(preset_params.get("top_k", 15))

    recent_resolutions = conn.execute(
        "SELECT e.source_id, e.target_id, n.claim "
        "FROM edges e JOIN nodes n ON n.id = e.source_id "
        "WHERE e.relation='resolves' AND n.is_current=1 "
        "ORDER BY n.created_at DESC LIMIT 30"
    ).fetchall()

    candidates: list[dict] = []
    seen_questions: set[str] = set()
    for resolver_id, resolved_id, resolution_claim in recent_resolutions:
        if not resolution_claim:
            continue
        # top_k=5 here is intentionally narrower than DEDUP_TOP_K — per-resolution
        # inner lookup, want the few best question candidates per resolution.
        matches = _semantic_search(
            conn, resolution_claim, top_k=5,
            min_similarity=threshold,
            type_filter={"question"},
            importance_threshold=0.0,
        )
        for m in matches:
            if m["id"] == resolved_id or m["id"] in seen_questions:
                continue
            row = conn.execute(
                "SELECT status FROM nodes WHERE id=?", (m["id"],)
            ).fetchone()
            if not row or row[0] != "open":
                continue
            seen_questions.add(m["id"])
            candidates.append({
                "id": m["id"],
                "claim": (m["claim"][:160] + "...") if m["claim"] and len(m["claim"]) > 160 else m["claim"],
                "score": m["similarity"],
                "echo_resolution": {
                    "resolver_id": resolver_id,
                    "originally_resolved": resolved_id,
                },
                "suggested_action": (
                    f"Inspect resolver {resolver_id} and consider whether the "
                    f"same logical chain applies: engram_resolve({m['id']}, ...)."
                ),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


PATTERN_QUERY_REGISTRY: dict[str, callable] = {
    "contradiction_obsolescence_ready": _pattern_contradiction_obsolescence_ready,
    "open_question_answerable":          _pattern_open_question_answerable,
    "stale_load_bearing":                _pattern_stale_load_bearing,
    "cornerstone_candidate":             _pattern_cornerstone_candidate,
    "tainted_still_valid":               _pattern_tainted_still_valid,
    "recent_resolution_echo":            _pattern_recent_resolution_echo,
}


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
def engram_query_pattern(
    pattern_name: str,
    preset: str = "balanced",
    cosine_threshold_override: float = -1.0,
    top_k_override: int = -1,
    min_confidence_override: float = -1.0,
    summary_top_k: int = 3,
) -> str:
    """Run a named compositional graph-pattern query.

    The tool does mechanical work (graph traversal + ranking); the agent
    applies judgment to the ranked candidates. Each call appends a tuple
    to ~/.engram/pattern_query_telemetry.jsonl for empirical preset
    calibration. See active-work/engram-query-pattern-design-2026-05-06.md
    for the full design.

    Args:
        pattern_name: One of the registered patterns:
            - contradiction_obsolescence_ready: active contradictions
              where one side is retracted/superseded, ranked by how
              unambiguous the obsolescence signal is.
            - open_question_answerable: open questions with a derivation
              chain nearby (semantic) that may resolve them.
            - stale_load_bearing: high-importance + low-recall non-
              cornerstone nodes (re-engagement candidates).
            - cornerstone_candidate: heavily-cited high-importance non-
              cornerstone nodes (anchoring candidates).
            - tainted_still_valid: tainted derivations whose substantive
              claim may survive the upstream retraction.
            - recent_resolution_echo: still-open questions semantically
              similar to a recent resolution's claim (echo candidates).
        preset: One of high_precision / balanced (default) / high_recall.
            Bundles (cosine_threshold, top_k, min_confidence). The
            placeholder values are deferred for calibration discussion;
            override individual parameters with the *_override args.
        cosine_threshold_override: If >= 0, overrides the preset's
            cosine_threshold. Default -1 (use preset).
        top_k_override: If >= 0, overrides the preset's top_k. Default -1.
        min_confidence_override: If >= 0, overrides the preset's
            min_confidence. Default -1.
        summary_top_k: How many top candidates get a recall_summary (Tier 1).
            The remaining candidates get recall_keywords only (Tier 2). Default 3.
            Clamped to [0, candidate_count].

    Returns:
        JSON with {pattern_name, preset, results, total_matches}.
        Tier 1 (top summary_top_k entries): {"id": ..., "summary": ...}
        Tier 2 (remainder): {"id": ..., "keywords": [...]} or {"id": ...}
        Telemetry log writes are unaffected by the tier transformation.
    """
    if pattern_name not in PATTERN_QUERY_REGISTRY:
        return json.dumps({
            "error": f"Unknown pattern_name '{pattern_name}'. "
                     f"Registered: {sorted(PATTERN_QUERY_REGISTRY.keys())}.",
        })
    if preset not in PATTERN_QUERY_PRESETS:
        return json.dumps({
            "error": f"Unknown preset '{preset}'. "
                     f"Available: {sorted(PATTERN_QUERY_PRESETS.keys())}.",
        })

    params = dict(PATTERN_QUERY_PRESETS[preset])
    if cosine_threshold_override >= 0:
        params["cosine_threshold"] = cosine_threshold_override
    if top_k_override >= 0:
        params["top_k"] = top_k_override
    if min_confidence_override >= 0:
        params["min_confidence"] = min_confidence_override

    conn = _get_db()
    try:
        impl = PATTERN_QUERY_REGISTRY[pattern_name]
        candidates = impl(conn, params)
    finally:
        conn.close()

    # Telemetry log: internal eval surface — preserved verbatim (ob_1500 discipline).
    # The tier transformation applies to the agent-facing return shape only.
    _pattern_telemetry_log(pattern_name, preset,
                            {"params_used": params}, candidates)

    # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
    # engram_query_pattern has no free-text query; query_text_len is the
    # pattern_name length as a proxy. source_class_filter and type_filter
    # are not parameters of this tool — emitted as None. See phase-3 F1
    # handoff ambiguous-decisions log.
    emit_if_initialized(
        event_type="engram.tool.engram_call",
        level=1,
        data={
            "tool_name": "engram_query_pattern",
            "query_text_len": len(pattern_name),
            "result_count": len(candidates),
            "type_filter": None,
            "source_class_filter": None,
            "pattern_name": pattern_name,
            "preset": preset,
        },
    )

    # Apply tiered render after pattern-internal ranking is complete.
    # Clamp summary_top_k to [0, len(candidates)].
    effective_top_k = max(0, min(summary_top_k, len(candidates)))
    tiered = _build_tiered_results(candidates, effective_top_k)

    return json.dumps({
        "pattern_name": pattern_name,
        "preset": preset,
        "results": tiered,
        "total_matches": len(candidates),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _ensure_data_dir()
    # Initialize ~/.engram/.git for the durable version-controlled snapshot.
    # Best-effort: if git is missing or init fails, _git_available stays False
    # and engram_nap / engram_advance_turn will report version_control.git_committed=False.
    _init_git()

    # Pre-warm embedding model if already cached locally.
    # If not cached, disable embeddings to avoid blocking MCP with a download.
    if _embedder.is_available():
        import sys
        emb_config = _get_embedding_config()
        model_name = emb_config.get("model", DEFAULT_EMBEDDING_MODEL)
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
                # Model is cached — safe to load without network
                _embedder._load_model(model_name)
                if _embedder._model is not None:
                    print(f"[engram] Embedding model '{model_name}' loaded.", file=sys.stderr)
        except ImportError:
            pass  # huggingface_hub not available, let it try normally
        except Exception:
            _embedder._failed_models.add(model_name)

    mcp.run()
