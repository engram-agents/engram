"""engram_core — shared state, DB layer, and write primitives for the ENGRAM MCP server.

Extracted from server.py in #872 wave 1. HOUSE RULES (spec D3):
- Mutable module state (path globals set by _configure_paths; runtime flags)
  must be accessed via `import engram_core as core; core.NAME` — NEVER via
  `from engram_core import NAME` (name-binding holds a stale copy after
  _configure_paths / runtime flips).
- This module must not import server.py or any family module (acyclic).
- Helpers used by 3+ tool families get promoted here in the family wave that
  first needs them out of server.py (rolling promotion; spec D6 note).

Checkpoint/backup layers (both written in _commit_snapshot on every nap):
- knowledge.sql — embedding-stripped SQL text dump, committed to git (line-diff friendly).
- db-backup/knowledge-YYYYMMDD.db — binary hot-copy for fast point-in-time restore
  without re-running the full pipeline. 7-day rotating window (~84 MB). Best-effort;
  never blocks the nap. db-backup/ should be listed in ~/.engram/.gitignore (binary,
  large, not line-diff friendly).
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
import threading
import time
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

from engram_confidence import (  # noqa: E402
    CONFIDENCE_MAP,
    PREDICTIVE_CONFIDENCE_CAP,
    SOURCE_CLASS_CONFIDENCE_DISCOUNT,
    CONJECTURE_CONFIDENCE_DEFAULT,
    CONJECTURE_CONFIDENCE_MIN,
    CONJECTURE_CONFIDENCE_MAX,
    REASONING_TYPES,
    REASONING_CLASS,
    REASONING_DISCOUNT,
    ABDUCTIVE_CONFIDENCE_CAP,
)


# #1682: belt-and-suspenders against HuggingFace Hub's online etag-check
# retry ladder (6 x 10s + backoff ~= 83s stall, recurred 4x on a slow/
# unreachable HF Hub). EmbeddingManager._load_model() below now loads
# cached models fully offline (local_files_only=True), which already avoids
# any HF Hub network call — this env var is a second line of defense so
# that even a residual online etag-check (e.g. a first-time download, or a
# future code path that doesn't set local_files_only) times out in ~1s
# instead of ~83s. Set before any huggingface_hub / sentence_transformers
# import (both are imported lazily, inside functions, later in this
# module). setdefault() so an operator override in the environment wins.
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "1")


try:
    import sqlite_vec as _sqlite_vec
    _SQLITE_VEC_IMPORT_OK = True
except Exception:  # pragma: no cover
    _sqlite_vec = None
    _SQLITE_VEC_IMPORT_OK = False
    # Loud, not silent: a missing vec0 backend means semantic search quietly
    # degrades to the slow O(N) cosine path AND the #720 backup will crash on any
    # vec_nodes table it tries to drop. This degradation went unnoticed on real
    # installs (the migration venv-create omitted sqlite-vec) — so announce it.
    import sys as _sys
    print(
        "[engram] sqlite-vec unavailable — semantic search is on the slow "
        "O(N) pure-Python cosine fallback; install sqlite-vec==0.1.9 in this "
        "interpreter to enable the vec0 KNN index (see #728).",
        file=_sys.stderr,
    )


_VEC_BACKEND_AVAILABLE = _SQLITE_VEC_IMPORT_OK


class EmbeddingManager:
    """Manages embedding model loading and computation.

    Lazy-loads the model on first use. Stores the model name in the KG's
    config to prevent mixing embeddings from different models.

    #1675: FastMCP dispatches sync tools on a real threadpool, so two
    concurrent first-touch tool calls could previously race into
    _load_model() at once — double-loading the model (wasted work / memory)
    or, worse, one thread observing self._model set to a partially-
    constructed SentenceTransformer from the other thread (no documented
    thread-safety guarantee for concurrent construction). _lock guards the
    lazy-load path with double-checked locking, and also serializes
    .encode() calls: sentence-transformers documents no thread-safety
    contract for concurrent encode() on one model instance, so correctness
    comes first here — concurrent embed() calls now queue rather than race.
    Known follow-up: this trades away encode() throughput under concurrent
    load (only one embed at a time, process-wide); if that becomes a
    bottleneck, look at either a small connection-pool-style set of model
    instances or moving encode() off the request thread entirely.
    """

    def __init__(self):
        self._model = None
        self._model_name = None
        self._failed_models = set()  # cache download failures
        self._lock = threading.Lock()

    def _load_model(self, model_name: str):
        """Load the sentence-transformers model.

        Offline-first (#1682): tries local_files_only=True so a cached model
        never triggers HF Hub's online etag-check (source of an ~83s stall
        when HF Hub is slow/unreachable). Falls back to a one-time online
        download on a genuine cache-miss — auto-downloads from HuggingFace.
        """
        # Fast path: no lock if already loaded for this model_name.
        if self._model is not None and self._model_name == model_name:
            return
        if model_name in self._failed_models:
            return  # already tried and failed
        with self._lock:
            # Re-check inside the lock (double-checked locking) — another
            # thread may have finished loading (or failed) while we waited.
            if self._model is not None and self._model_name == model_name:
                return
            if model_name in self._failed_models:
                return
            try:
                from sentence_transformers import SentenceTransformer
                try:
                    # #1682: offline-first load. A cached model loads with
                    # ZERO HuggingFace Hub network traffic — no online
                    # etag-check, so the ~83s stall (HF Hub slow/unreachable)
                    # cannot happen on the common warm-cache path (every
                    # daemon warmup / MCP server restart after the first
                    # successful load).
                    self._model = SentenceTransformer(model_name, local_files_only=True)
                except Exception:
                    # Cache-miss: this install does NOT pre-download the
                    # embedder model as a discrete install step (verified —
                    # no install script calls SentenceTransformer(...); the
                    # daemon's own background pre-warm in server.py only
                    # loads an ALREADY-cached model, it does not download).
                    # The genuine first-time download happens here, lazily,
                    # on first real use — README-AGENT.md's documented
                    # "first cold launch loads the sentence-transformer
                    # model (~80 MB); later launches hit the warm cache"
                    # behavior. Fall back to a one-time online download so
                    # a fresh install keeps working; every subsequent load
                    # hits the local_files_only path above.
                    import sys as _sys
                    print(
                        f"[engram] Embedding model '{model_name}' not in "
                        f"local HF cache — downloading once (online); "
                        f"subsequent loads are offline.",
                        file=_sys.stderr,
                    )
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
        # Serialize encode() calls (see class docstring #1675) — no
        # documented thread-safety guarantee for concurrent encode() on one
        # sentence-transformers model instance.
        with self._lock:
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
        with self._lock:
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


_embedder = EmbeddingManager()


DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


DATA_DIR = Path(os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"))


DB_PATH = DATA_DIR / "knowledge.db"


SNAPSHOT_PATH = DATA_DIR / "graph_snapshot.md"


CONFIG_PATH = DATA_DIR / "config.json"


LOG_PATH = DATA_DIR / "session_log.md"


FEELING_NUDGE_MARKER = DATA_DIR / "feeling-nudge-active.json"


FEELING_NUDGE_TTL_TURNS = 5


FEELING_NUDGE_SOURCES = {"post_compact", "nap_checkpoint", "dream_review"}


_SANDBOX_ACTIVE = False


_walguard_last_check: float = 0.0


_WALGUARD_CHECK_INTERVAL: float = 30.0


_walguard_startup_done: bool = False


_walguard_disabled_logged: bool = False


# ── #1669: one-shot guard for the per-call backfill/DAG-check block ────────
# cProfile evidence (2026-07-06, umbrella #1668) showed _get_db() spending
# 65-96% of its time re-running an unguarded backfill/migration/DAG-check
# block on EVERY call, in contrast to the schema migrations in the same
# function that correctly gate on PRAGMA user_version. _db_setup_done_paths
# tracks which resolved DB paths have already had that block run
# successfully IN THIS PROCESS — intentionally NOT persisted to disk, so a
# process restart re-runs the block once (safe; the block is idempotent).
# A path is added to the set ONLY after the block completes without
# exception, so a failure (e.g. a transient sqlite lock) leaves the path
# unmarked and the block retries on the next _get_db() call.
_db_setup_done_paths: set[str] = set()


_db_setup_lock = threading.Lock()


def _configure_paths(data_dir: str | Path) -> None:
    """Reconfigure ALL module-level path globals from a single data_dir.

    This is the ONLY safe way to redirect ENGRAM to a different directory.
    Manually patching individual path variables is fragile — if a new path
    is added to server.py and not patched, writes leak to production.

    Called at module load time (with env var / default), and by EngramClient
    and engram_sandbox() for test isolation.

    Incident context (the test-isolation lesson, the test-isolation incident): a test that manually patched
    DATA_DIR and DB_PATH contaminated the production engram with 5 test nodes.
    """
    global DATA_DIR, DB_PATH, SNAPSHOT_PATH, CONFIG_PATH, LOG_PATH
    global FEELING_NUDGE_MARKER, ERROR_INCIDENTS_PATH, CORNERSTONE_ANCHORS_PATH
    global PRINCIPLE_TRIGGERS_PATH
    global _walguard_last_check, _walguard_startup_done, _walguard_disabled_logged
    DATA_DIR = Path(data_dir)
    DB_PATH = DATA_DIR / "knowledge.db"
    SNAPSHOT_PATH = DATA_DIR / "graph_snapshot.md"
    CONFIG_PATH = DATA_DIR / "config.json"
    LOG_PATH = DATA_DIR / "session_log.md"
    FEELING_NUDGE_MARKER = DATA_DIR / "feeling-nudge-active.json"
    ERROR_INCIDENTS_PATH = str(DATA_DIR / "error_incidents.json")
    CORNERSTONE_ANCHORS_PATH = str(DATA_DIR / "cornerstone_anchors.json")
    PRINCIPLE_TRIGGERS_PATH = str(DATA_DIR / "principle_triggers.json")
    # Reset walguard state when paths change (e.g. test isolation)
    _walguard_last_check = 0.0
    _walguard_startup_done = False
    _walguard_disabled_logged = False


USE_ALPHA = {
    # Tier 1 — deep cognitive load
    "derive":          0.15,
    "supersede":       0.15,
    "contradict":      0.15,
    "resolve":         0.15,
    "lesson_incident":   0.15,  # legacy key; preserved for backward-compat
    "register_exemplar": 0.15,  # unified key (used by engram_register_exemplar)
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


UTIL_BETA = 0.10


IMP_BETA = 0.005


TIER1_MULTIPLIER = 30   # raw-retrieval pool size per source = top_k × 30


TIER2_MULTIPLIER = 4    # composite-shrink target           = top_k × 4


MMR_LAMBDA = 0.9


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


SPECIAL_POOL_CAP = 10


FTS_SIM_FLOOR = 0.30


FTS_BUMP_NORMALIZER = 16.0   # sqrt(this) = 4.0; bump = sqrt(|bm25|) / 4.0


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

    Incident context (the test-isolation lesson): built after a test contaminated production
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
                    "confidence_map": CONFIDENCE_MAP,
                    "memory": {
                        "decay_base": 1.014,
                        "current_turn": 0,
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


def _as_csv(v) -> str:
    """Coerce a list or None to a comma-string; pass strings through unchanged."""
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return v or ""


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


CLAIM_BEARING_TYPES = {
    "observation_factual",
    "observation_predictive",
    "derivation",
    "theory",
    "axiom",
    "conjecture",
    "lesson",
}

# Valid TARGET types for the `instantiates` relation (#530): the principle-family
# nodes a claim-bearing source can realize. Lessons are deliberately ABSENT —
# incident → lesson membership is `exemplifies` territory (engine-coupled to the
# tripwire cache); _add_edge_impl rejects lesson targets with a pointer there.
INSTANTIATES_TARGET_TYPES = {
    "goal",
    "cornerstone",
    "definition",
    "axiom",
}


VALID_RELATIONS = {
    "cites",
    "supported_by",
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
    "instantiates",  # Achievement-shaped relevance marker (#530, PR 3 of #510):
                     # source (claim-bearing node) realizes / implements /
                     # is-an-instance-of target (goal, cornerstone, definition,
                     # axiom). Distinct from `serves` (intent-shaped: work
                     # contributes TOWARD a goal) and from `exemplifies`
                     # (incident → lesson, engine-coupled to the tripwire
                     # cache). Lesson targets are mechanically REJECTED with a
                     # pointer to `exemplifies` — the boundary is a gate, not a
                     # doc. Pure relevance: no cascade, no confidence
                     # propagation, DAG-exempt (realization is not a temporal
                     # dependency — a goal may be articulated after the work
                     # that realizes it). The dream-master's post-hoc wiring
                     # tool; also the axiom-grounding shape for practice
                     # observations ("ob_X instantiates ax_Y").
}


EDGE_CLASSIFICATIONS = {
    "about": {
        "cascade": False,
        "dag_check": False,
        "removable": True,
        "addable_after_creation": True,
        "provenance": False,
    },
    "cites": {
        "cascade": False,
        "dag_check": True,
        "removable": False,    # provenance load-bearing; removal would orphan claims
        "addable_after_creation": False,  # legacy dual-use; PR 3 introduces new relation for relevance-marking
        "provenance": True,
    },
    "contradicts": {
        "cascade": False,
        "dag_check": False,    # advisory check exempts contradicts (the contradicts-dag-exempt observation)
        "removable": False,    # structural commitment; alters epistemic stance
        "addable_after_creation": False,
        "provenance": False,
    },
    "derives_from": {
        "cascade": True,       # taint/stale propagation
        "dag_check": True,
        "removable": False,    # cascade-bearing
        "addable_after_creation": False,
        "provenance": False,
    },
    "exemplifies": {
        "cascade": False,
        "dag_check": False,    # advisory check exempts exemplifies (lesson edges, the exemplifies-dag-exempt observation)
        "removable": True,
        "addable_after_creation": True,
        "provenance": False,
    },
    "instantiates": {
        "cascade": False,      # pure relevance marker — no confidence propagation
        "dag_check": False,    # realization is not a temporal dependency (see VALID_RELATIONS note)
        "removable": True,
        "addable_after_creation": True,
        "provenance": False,
    },
    "resolves": {
        "cascade": False,      # not a confidence-propagating edge
        "dag_check": False,    # back-reference edge: resolver legitimately pre-dates the question
                               # (a question filed and immediately resolved by a pre-existing node
                               # has resolver.created_at < question.created_at — correct, not a
                               # violation). Exemption added 2026-06-10, issue #1076.
        "removable": False,    # structural commitment
        "addable_after_creation": False,
        "provenance": False,
    },
    "retracts": {
        "cascade": True,       # cascade marker for downstream taint
        "dag_check": True,
        "removable": False,
        "addable_after_creation": False,
        "provenance": False,
    },
    "serves": {
        "cascade": False,
        "dag_check": False,    # same shape as instantiates: a goal can be articulated after the node
                               # that serves it — serves.source legitimately pre-dates serves.target.
                               # Exemption added 2026-06-10, issue #1076 (three-tier edge taxonomy).
        "removable": True,
        "addable_after_creation": True,
        "provenance": False,
    },
    "subtask_of": {
        "cascade": False,
        "dag_check": True,
        "removable": True,
        "addable_after_creation": True,
        "provenance": False,
    },
    "supersedes": {
        "cascade": True,
        "dag_check": True,
        "removable": False,    # structural mutation; use engram-surgical for emergencies
        "addable_after_creation": False,
        "provenance": False,
    },
    "supported_by": {
        "cascade": True,
        "dag_check": True,
        "removable": False,
        "addable_after_creation": False,
        "provenance": False,
    },
    "tensions": {
        "cascade": False,
        "dag_check": False,    # relational tier (dependency/relational/classification): symmetric/cross-temporal
                               # like contradicts (already exempt). A tension between nodes has no causal
                               # direction; either can pre-date the other legitimately.
                               # Exemption added 2026-06-10, issue #1076 (three-tier edge taxonomy).
        "removable": True,
        "addable_after_creation": True,
        "provenance": False,
    },
}


# ── Edge tier taxonomy ───────────────────────────────────────────────────────
#
# Three-tier classification of edge relations per the agent's edge taxonomy
# (the three-tier edge taxonomy: dependency / relational / classification).
# Maintained as a SEPARATE dict from
# EDGE_CLASSIFICATIONS so that existing consumers of that dict (which iterate
# its schema of {cascade, dag_check, removable, addable_after_creation,
# provenance}) are not affected.
#
# Tier→dag_check expectation:
#   dependency     ⇒ dag_check=True   (causal / provenance chain; temporal order matters)
#   relational     ⇒ dag_check=False  (symmetric or cross-temporal; no causal direction)
#   classification ⇒ dag_check=False  (realization / membership; no temporal constraint)
#
# NOTE on subtask_of: tiered "dependency" (dag_check=True) deliberately.
# A subtask generally post-dates its parent goal/task, making the DAG check
# correct in the common case. The rare task-reorg inversion (old task becomes
# subtask of a newer umbrella) has 0 current violations, so it stays checked.
# This is a watch item, not a divergence requiring acknowledgment.
#
# AUDIT SURFACE (not enforcement): test_edge_tier_audit.py uses this map to
# warn on unintended drift between dag_check flags and tier expectations.
# Divergence emits a warnings.warn for human review — never a hard failure.
# A relation MAY intentionally diverge (see EDGE_TIER_DIVERGENCE_ACK below).
EDGE_TIERS: dict[str, str] = {
    # dependency tier: causal / provenance edges; dag_check=True expected
    "cites":        "dependency",
    "derives_from": "dependency",
    "retracts":     "dependency",
    "supersedes":   "dependency",
    "supported_by": "dependency",
    "subtask_of":   "dependency",
    # relational tier: symmetric / cross-temporal; dag_check=False expected
    "contradicts":  "relational",
    "tensions":     "relational",
    "about":        "relational",
    "resolves":     "relational",   # epistemic-stance edge: a resolution closes a question/contradiction (pairs with contradicts), not a realization/membership
    # classification tier: realization / membership; dag_check=False expected
    "instantiates": "classification",
    "serves":       "classification",
    "exemplifies":  "classification",
}

# Intentional-divergence acknowledgment table.
# If a relation's dag_check flag intentionally diverges from its tier's
# expectation, add it here with a short reason string. The audit test
# (test_edge_tier_audit.py) skips emitting a warning for acknowledged entries,
# preserving the ability to intentionally diverge without noisy CI output.
# Currently empty — there are no intentional divergences.
EDGE_TIER_DIVERGENCE_ACK: dict[str, str] = {
    # "<relation>": "<reason for intentional divergence>"
    # Example (hypothetical):
    #   "about": "temporarily dag_check=True pending refactor #NNNN"
}

DAG_EXEMPT_RELATIONS = frozenset(
    r for r, c in EDGE_CLASSIFICATIONS.items() if not c["dag_check"]
)


_ADDABLE_AFTER_CREATION_RELATIONS = frozenset(
    r for r, c in EDGE_CLASSIFICATIONS.items() if c["addable_after_creation"]
)


DEFAULT_RESOLUTION_THRESHOLD = 0.7


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
    "trust_signal": "ts",
}


GIT_EXE = os.environ.get(
    "ENGRAM_GIT_EXE",
    r"C:\Program Files\Git\cmd\git.exe" if os.name == "nt" else "git",
)


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


_git_available = False


def _init_git():
    """Initialize git repo. Called once at startup with a generous timeout.

    Set ENGRAM_DISABLE_GIT=1 to opt out entirely. This is the clean fallback for
    environments where git resolution is broken — historically the Claude Desktop
    Windows app picked up the WSL git binary instead of the Windows one and the
    versioning layer had to be disabled. With ENGRAM_DISABLE_GIT=1, _git_available
    stays False and engram_nap / engram_advance_turn silently skip committing
    while still saving the session log and graph state to disk.

    Also ensures a canonical .gitignore that excludes binary files, transient
    SQLite files, and other junk that must never be tracked. Idempotent —
    safe to call on an already-initialized repo.
    """
    global _git_available, GIT_TIMEOUT
    if os.environ.get("ENGRAM_DISABLE_GIT"):
        import sys
        print("[engram] ENGRAM_DISABLE_GIT set — version control disabled by user opt-out.", file=sys.stderr)
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Write .gitignore before git init so it's respected from the first commit.
    # Patterns verified 2026-06-02 against real files that slipped through before
    # this fix (*.db tracked, *.bak tracked, *.log tracked, etc.).
    _ensure_engram_gitignore(DATA_DIR)
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


def _ensure_engram_gitignore(data_dir: "Path") -> None:
    """Write (or refresh) DATA_DIR/.gitignore with the canonical ENGRAM patterns.

    Idempotent: if the file already contains ALL required patterns it is left
    unchanged. If it is absent or missing patterns, the canonical content is
    written (replacing any prior content — the canonical set is the authoritative
    truth, not a patch-on-top).

    Patterns rationale (each earned by a real file that slipped through):
      *.db / *.db-shm / *.db-wal / *.db-journal — SQLite binary + WAL files; binary,
          never diff-able, must never be tracked (restoring from .sql is the intent).
      *?mode=* — URI-junk files from botched sqlite opens (e.g. knowledge.db?mode=ro).
      *.bak / *.bak.* / config.json.bak* — timestamped backups; large, binary, churn.
      *.backfill-snapshot-* — temporary backfill snapshots.
      *.log — log files; noise, never part of the backup.
      marketplace/ venv/ __pycache__/ *.pyc — runtime / build artifacts.

    NOT excluded: knowledge.sql (the text dump), graph_snapshot.md, session_log.md,
    config.json, warm-briefing.md, diary/*, history/*, *.json identity files — these
    ARE the backup.
    """
    _GITIGNORE_PATTERNS = [
        "*.db",
        "*.db-shm",
        "*.db-wal",
        "*.db-journal",
        "*?mode=*",
        "*.bak",
        "*.bak.*",
        "config.json.bak*",
        "*.backfill-snapshot-*",
        "*.log",
        "marketplace/",
        "venv/",
        "__pycache__/",
        "*.pyc",
    ]
    gitignore_path = data_dir / ".gitignore"
    canonical = "\n".join(_GITIGNORE_PATTERNS) + "\n"
    try:
        if gitignore_path.exists():
            existing = gitignore_path.read_text(encoding="utf-8")
            # Check if ALL required patterns are present (exact-line match)
            existing_lines = {l.strip() for l in existing.splitlines()}
            if all(p in existing_lines for p in _GITIGNORE_PATTERNS):
                return  # Already complete — leave it alone
        gitignore_path.write_text(canonical, encoding="utf-8")
    except OSError:
        pass  # Best-effort — if we can't write it, git init still proceeds


def _ensure_data_dir():
    """Create data directory and config file if needed. Git is handled separately."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        default_config = {
            # Schema v2 (2026-05-05): adds cadence block. Migration from
            # v1 → v2 is in tools/migration/migrate_config_v2.py for existing installs.
            "schema_version": 2,
            # Empty by default — users set their own trusted sources via the
            # config tab (#1228); source-based confidence calibration is
            # future-work (#1230).
            "trust_pool": [],
            "confidence_map": CONFIDENCE_MAP,
            "predictive_confidence_cap": PREDICTIVE_CONFIDENCE_CAP,
            "chain_decay": 0.95,
            "corroboration_decay": 0.98,
            "resolution_confidence_threshold": DEFAULT_RESOLUTION_THRESHOLD,
            "memory": {
                "decay_base": 1.014,
                "current_turn": 0,
                "tier2_max_nodes": 1000,
            },
            "embedding": {
                "model": DEFAULT_EMBEDDING_MODEL,
                "enabled": True,
            },
            "cadence": {
                # Drowsiness meter + in-session auto-sleep scheduler tunables.
                # drowsiness_caution_pct / drowsiness_urgent_pct trigger
                # context_tracker warnings as the session fills.
                # auto_sleep_enabled / auto_sleep_time are consumed by the
                # SessionStart hook to register a nightly in-session CronCreate
                # that fires /engram-sleep at the configured time.
                # Changes take effect next session (restart_required).
                "drowsiness_caution_pct": 80,
                "drowsiness_urgent_pct": 90,
                "auto_sleep_enabled": False,
                "auto_sleep_time": "03:00",
            },
            # Standpoint v3 null=self (D1 §2): the install's OWN training lineage
            # (provider:family, e.g. "anthropic:opus"). Read by _self_lineage()
            # so an unmarked observation counts as the filer's own lineage, which
            # is what makes the standpoint/F-S advisory fire on own-unmarked
            # derivations. Default "" = feature ships DARK (safe degradation —
            # gate stays closed, same as pre-feature); the first-session skill
            # prompts the user to set it. NOT auto-detected — the agent declares it.
            "self_lineage": "",
            # Fairy delegation policies — consumed by SessionStart hook per-decision.
            # Three modes per policy: "explicit", "auto", "always".
            # "auto" uses the named judgement skill; "explicit" only spawns on
            # direct user request; "always" unconditionally delegates to fairy.
            # Changes take effect immediately (read per-decision, restart_required=False).
            "coder_fairy_policy": "auto",
            "reviewer_fairy_policy": "auto",
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


def _best_resolver_for(conn: sqlite3.Connection, target_id: str):
    """Return the argmax-confidence current, non-retracted resolver for target_id.

    Queries the resolves edge set and returns a sqlite3.Row with columns
    (id, confidence) for the highest-confidence qualifying resolver, or None
    if no such resolver exists. Shared helper used by:
      - _resolve_impl normal path (engram_revision.py)
      - _resolve_impl no_op self-heal (engram_revision.py)
      - _backfill_resolved_by (below)

    Does NOT commit. Caller manages the transaction.
    """
    return conn.execute(
        """SELECT n.id, n.confidence FROM edges e
           JOIN nodes n ON e.source_id = n.id
           WHERE e.target_id = ? AND e.relation = 'resolves'
           AND n.is_current = 1 AND n.status != 'retracted'
           ORDER BY n.confidence DESC LIMIT 1""",
        (target_id,),
    ).fetchone()


def _backfill_resolved_by(conn: sqlite3.Connection) -> int:
    """One-time pass: write resolved_by for resolved nodes that are missing it.

    Targets nodes with resolved_by IS NULL and a resolution status — the
    pre-#759 state for contradictions and other resolvable types. For each,
    runs the argmax-confidence resolver lookup via _best_resolver_for and
    writes resolved_by. Idempotent: only touches rows with NULL resolved_by.

    Applies to all resolvable-status nodes (not just contradictions) for
    thoroughness. The named defect class is contradictions (pre-#759 gap).

    Returns the count of rows updated. Commits only if rows were updated.
    """
    RESOLVED_STATUSES = ("resolved", "partially_resolved", "confirmed",
                         "partially_confirmed", "refuted", "partially_refuted",
                         "supported", "inconclusive")
    placeholders = ",".join("?" * len(RESOLVED_STATUSES))
    rows = conn.execute(
        f"SELECT id FROM nodes "
        f"WHERE resolved_by IS NULL AND status IN ({placeholders})",
        RESOLVED_STATUSES,
    ).fetchall()
    if not rows:
        return 0
    updated = 0
    for r in rows:
        node_id = r["id"]
        best = _best_resolver_for(conn, node_id)
        if best is None:
            continue  # no qualifying resolver in edges — skip
        conn.execute(
            "UPDATE nodes SET resolved_by = ? WHERE id = ?",
            (best["id"], node_id),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


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


def _db_missing_message() -> str:
    """Actionable error message when the DB file is missing.

    Surfaced via _get_db() raising on the first tool call when no DB exists at
    DATA_DIR/knowledge.db. The plugin packaging has no installer-script step,
    so this is the user's signal that engram-first-session needs to bootstrap.
    """
    engram_home_env = os.environ.get("ENGRAM_HOME", "<unset; default ~/.engram>")
    return (
        f"ENGRAM cannot start: knowledge.db not found at {DB_PATH}\n\n"
        "This usually means one of:\n"
        "  - Fresh install — run the engram-first-session skill to bootstrap\n"
        "    (your agent will walk you through it in the next conversation).\n"
        "  - ENGRAM_HOME is pointing at the wrong directory — check the env var\n"
        f"    (current ENGRAM_HOME: {engram_home_env}).\n"
        "  - You moved your install — symlink ~/.engram to your data directory.\n\n"
        "If you intend to start fresh in a non-standard location, set ENGRAM_HOME\n"
        "explicitly and re-run the engram-first-session skill."
    )


def _seed_missing_message() -> str:
    """Actionable error message when the DB exists but has no seed nodes.

    Raised when SELECT COUNT(*) FROM nodes WHERE type='axiom' returns 0 —
    implies bootstrap.py never completed against this DB (either it crashed
    mid-run or the user manually created an empty knowledge.db).
    """
    return (
        f"ENGRAM cannot start: knowledge.db exists at {DB_PATH} but has no seed nodes.\n\n"
        "This usually means a bootstrap was started but never completed.\n"
        "Run the engram-first-session skill to complete the bootstrap, or\n"
        f"delete {DB_PATH} (verify it's empty first!) and re-run first-session\n"
        "for a clean retry."
    )


def _walguard_startup_clear(conn: sqlite3.Connection) -> None:
    """First-init check: clear a stale degraded marker if the DB is now healthy.

    Called once per server lifetime (guarded by _walguard_startup_done flag).
    If a degraded marker exists AND detect_shm_displacement() returns None AND
    PRAGMA integrity_check returns 'ok', the marker is removed and a log line
    is emitted.  If integrity_check fails, the marker is left in place and a
    loud error is logged.

    Never raises — any internal failure is logged and swallowed.
    """
    import sys
    try:
        import engram_walguard as _wg
        marker = _wg.read_degraded_marker(DATA_DIR)
        if marker is None:
            return  # no stale marker; nothing to do
        detection = _wg.detect_shm_displacement(str(DB_PATH))
        if detection is not None:
            # Still displaced — leave the marker and log loudly
            print(
                f"[walguard] CRITICAL: stale degraded marker present AND shm still displaced "
                f"(reason={detection.get('reason')}); leaving marker in place. "
                f"Do not run a hard checkpoint — request a clean MCP restart. (#786)",
                file=sys.stderr,
            )
            return
        # Displacement gone — verify integrity before clearing
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            ic_ok = len(rows) == 1 and rows[0][0] == "ok"
        except Exception as ie:
            print(
                f"[walguard] integrity_check raised during startup clear: {ie}; "
                f"leaving degraded marker in place. (#786)",
                file=sys.stderr,
            )
            return
        if ic_ok:
            _wg.clear_degraded_marker(DATA_DIR)
            print(
                "[walguard] degraded marker cleared after healthy restart. (#786)",
                file=sys.stderr,
            )
        else:
            print(
                f"[walguard] CRITICAL: integrity_check FAILED during startup; "
                f"leaving degraded marker in place. Check DB health before resuming. (#786)",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[walguard] startup clear error (non-fatal): {e!r} (#786)", file=sys.stderr)


def _run_walguard_check(conn: sqlite3.Connection) -> None:
    """Throttled WAL-guard check called from every tool dispatch via _get_db().

    No-ops unless ≥30 s have elapsed since the last check (keeps per-call cost
    near zero — two stat() calls plus a listdir that is amortised over the
    interval).  On detection: logs loudly, writes the degraded marker, attempts
    an emergency dump (checkpoint skipped — checkpointing under a split-brain
    WAL-index is the corruption vector, #786), and updates the marker's dump
    fields.

    Storm-gate (#786): displacement is a persistent condition.  To avoid firing
    a full SQL dump + git commit on every 30-second throttle window for the
    entire duration of the incident:
      - If a marker already exists with dump_committed=True and the same reason,
        skip the dump entirely UNLESS the last dump is >3600 s stale (hourly
        refresh keeps later writes captured without triggering a storm).
      - The original detected_at timestamp is always preserved for forensics.

    MUST NOT raise — any internal failure is logged and swallowed.  A broken
    guard must never take down tool dispatch.
    """
    global _walguard_last_check
    try:
        now = time.monotonic()
        if now - _walguard_last_check < _WALGUARD_CHECK_INTERVAL:
            return
        _walguard_last_check = now

        import engram_walguard as _wg
        detection = _wg.detect_shm_displacement(str(DB_PATH))
        if detection is None:
            return  # healthy

        import sys

        # ── Storm-gate: check for an existing committed marker (#786) ─────────
        # If we already committed a dump for this same displacement reason,
        # skip the expensive dump/commit unless the last dump is >1 hour old
        # (hourly refresh — keeps later writes captured without firing 720
        # commits over a 6-hour incident window).
        existing_marker = _wg.read_degraded_marker(DATA_DIR)
        original_detected_at: Optional[str] = None

        if existing_marker is not None and existing_marker.get("dump_committed") is True:
            if existing_marker.get("reason") == detection.get("reason"):
                # Same displacement reason — check hourly-refresh eligibility.
                original_detected_at = existing_marker.get("detected_at")
                last_dump_iso = existing_marker.get("last_emergency_dump")
                needs_refresh = True
                if last_dump_iso is not None:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        last_dump_ts = _dt.fromisoformat(last_dump_iso).timestamp()
                        needs_refresh = (time.time() - last_dump_ts) > 3600.0
                    except Exception:
                        pass  # unparseable timestamp → conservative: refresh

                if not needs_refresh:
                    # Dump is fresh; no storm action — return silently.
                    return

                # Dump is stale (>1 h): proceed with a refresh dump but log it.
                print(
                    f"[walguard] displacement still present (reason={detection.get('reason')}); "
                    f"last dump >1 h ago — refreshing emergency dump. (#786)",
                    file=sys.stderr,
                )
            else:
                # Reason changed (unusual but possible) — treat as a new event.
                original_detected_at = None

        if original_detected_at is None:
            # First detection: log loudly.
            print(
                f"[walguard] CRITICAL: WAL-index displacement detected "
                f"(reason={detection.get('reason')}, fd={detection.get('fd')}). "
                f"Writing degraded marker and attempting emergency dump. (#786)",
                file=sys.stderr,
            )

        # Write initial/refreshed marker (dump_committed=False until dump completes).
        # Pass detected_at to preserve the original timestamp on re-dumps.
        try:
            _wg.write_degraded_marker(
                DATA_DIR, detection, dump_info=None,
                detected_at=original_detected_at,
            )
        except Exception as me:
            print(f"[walguard] marker write failed: {me!r} (#786)", file=sys.stderr)

        # Emergency dump: skip_checkpoint=True to avoid the corruption vector
        dump_info: dict = {}
        try:
            dump_info = _commit_snapshot(
                conn,
                message="WAL-index displacement detected — securing dump (#786)",
                mode="emergency",
                skip_checkpoint=True,
            )
        except Exception as de:
            print(f"[walguard] emergency dump raised: {de!r} (#786)", file=sys.stderr)
            dump_info = {"git_committed": False, "error": str(de)}

        # Update marker with dump result; preserve original detected_at.
        try:
            _wg.write_degraded_marker(
                DATA_DIR, detection, dump_info=dump_info,
                detected_at=original_detected_at,
            )
        except Exception as me2:
            print(f"[walguard] marker update after dump failed: {me2!r} (#786)", file=sys.stderr)

    except Exception as e:
        import sys
        print(f"[walguard] _run_walguard_check error (non-fatal): {e!r} (#786)", file=sys.stderr)


def _walguard_degraded_banner() -> str:
    """Return the degraded-state warning string if the marker is present, else ''.

    Reads DATA_DIR unconditionally — NOT gated by ENGRAM_NO_DB_GUARDS.  Test
    code that writes a degraded marker into its isolated DATA_DIR will trigger
    this function and see the warning in any tool that calls it.  Tests that
    exercise banner-affected tools with a marker present should assert on the
    '_walguard_warning' key in the parsed JSON response dict.
    """
    try:
        import engram_walguard as _wg
        marker = _wg.read_degraded_marker(DATA_DIR)
        if marker is None:
            return ""
        ts = marker.get("detected_at", "unknown")
        sha = marker.get("dump_sha") or "FAILED"
        return (
            f"⚠️ SUBSTRATE DEGRADED: WAL-index displaced at {ts}; "
            f"emergency dump {sha}; "
            f"do not hard-kill the session — request a clean MCP restart. (#786)"
        )
    except Exception:
        return ""


def _run_db_one_time_setup(conn: sqlite3.Connection) -> None:
    """Run the one-shot backfill/migration/DAG-check block for `conn`.

    Extracted from _get_db() (#1669) so it can be gated by a once-per-
    process guard keyed on the resolved DB path (_db_setup_done_paths /
    _db_setup_lock, defined above _configure_paths) instead of re-running
    on every single _get_db() call. Nothing in this function's BEHAVIOR
    changed -- every migration/backfill here is already idempotent
    (schema migrations gate on PRAGMA user_version; the backfills gate on
    NULL/absence checks) -- only the CALL FREQUENCY changed, from 'every
    _get_db() call' to 'first call per resolved DB path, per process.'

    Caller contract: only invoke while holding _db_setup_lock, and only
    mark the path done (add to _db_setup_done_paths) AFTER this function
    returns without raising -- a failure here must leave the path
    unmarked so the next _get_db() call retries the whole block
    (guard-after-success, not guard-after-attempt).
    """
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

            -- Utility scoring (MemRL-inspired, the MemRL-inspired conjecture)
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
        # (the accumulation-hypothesis question's decisive test for the accumulation-hypothesis observation's accumulation hypothesis).
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
        # ── trust tier columns (Layer-1 trust-tier mechanism) ──
        # trust_tier: persisted tier for person (pn_*) nodes. One of:
        #   user_family, our_side, known_external, unknown, suspect.
        #   Backfilled to 'unknown' for all existing pn_* on startup.
        # trust_signal_*: interpretive metadata for trust_signal (ts_*) nodes.
        ("trust_tier", "TEXT"),
        ("trust_signal_kind", "TEXT"),
        ("trust_signal_polarity", "REAL"),
        ("trust_signal_weight", "REAL"),
        # ── standpoint (provenance-uniformity) columns ──
        # standpoint_author_id: persistent cross-session entity ID for who produced
        # the source claim ("who observes" axis). Used by _standpoint_cluster_key
        # to detect whether premises in a derivation trace to a single viewpoint.
        ("standpoint_author_id", "TEXT"),
        # standpoint_collection_id: corpus or work identity for the source
        # ("vantage" axis). Independent axis in per-axis standpoint cluster key.
        ("standpoint_collection_id", "TEXT"),
        # standpoint_override_tag: free-form standpoint label for when the computed
        # cluster key is insufficient (lab measurements, personal comms,
        # introspective self-reports).
        ("standpoint_override_tag", "TEXT"),
        # standpoint_lineage: training-lineage axis (v3), format "provider:family"
        # (e.g. "anthropic:opus"). The most load-bearing bias axis for AI-agent
        # premises: N same-lineage agents provide ~one witness of independent
        # corroboration on substrate-prior bias, for any N.
        ("standpoint_lineage", "TEXT"),
        # standpoint_architecture: cognitive architecture of the source's producer.
        # Enum: transformer | vision-spatial | embodied-sensorimotor |
        #       graph-neural | human | other.
        # Tracks architectural (not just training) diversity — Class A calibration
        # exposure: patterns orthogonal to transformer inductive bias are
        # undetectable by all-transformer-family premise sets.
        ("standpoint_architecture", "TEXT"),
        # fs_class: falsification-sensitivity native field (Phase 2).
        # NULL = Phase-1 proxy applies; "re-executable" | "frozen" = native.
        # No migration of existing rows — Phase-1 proxy covers all NULL cases.
        ("fs_class", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typedef}")

    # Trust-tier backfill: ensure all existing pn_* nodes have a non-null tier.
    # Idempotent — WHERE trust_tier IS NULL skips already-backfilled rows.
    conn.execute(
        "UPDATE nodes SET trust_tier = 'unknown' WHERE type = 'person' AND trust_tier IS NULL"
    )

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
                claim, quoted_text, interpretation, reported_state, trigger_text,
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
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation, new.reported_state, new.trigger_text);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS nodes_fts_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', old.rowid, old.claim, old.quoted_text, old.interpretation, old.reported_state, old.trigger_text);
        END
        """,
        # Retract trigger (#274, round-2 #280): remove retracted nodes from FTS.
        # Fires on AFTER UPDATE OF status when status transitions INTO 'retracted',
        # regardless of whether the node was current or already-superseded at the
        # time of retraction. The COALESCE guard prevents double-delete on idempotent
        # re-retracts (FTS5 'delete' magic-insert is NOT idempotent — double-delete
        # raises "database disk image is malformed").
        #
        # Design rev 2026-07-02 (#195): add AND OLD.is_current = 1.
        # In the new design nodes_fts_supersede_remove handles FTS eviction when
        # is_current flips 1→0; by the time a superseded node (is_current already 0)
        # is retracted, it is already absent from nodes_fts.  Without this guard the
        # retract trigger would attempt a second FTS 'delete' on an absent rowid,
        # raising "database disk image is malformed".
        #
        # FTS5 contentless-table mechanic: the 'delete' magic-insert requires
        # OLD column values because the contentless table doesn't store them.
        """
        CREATE TRIGGER IF NOT EXISTS nodes_retract_remove_from_fts
        AFTER UPDATE OF status ON nodes
        WHEN NEW.status = 'retracted' AND COALESCE(OLD.status, '') != 'retracted'
          AND OLD.is_current = 1
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', OLD.rowid,
                    COALESCE(OLD.claim, ''),
                    COALESCE(OLD.quoted_text, ''),
                    COALESCE(OLD.interpretation, ''),
                    COALESCE(OLD.reported_state, ''),
                    COALESCE(OLD.trigger_text, ''));
        END
        """,
        # Supersede trigger (#195, design rev 2026-07-02): remove superseded nodes
        # from the FTS search index when is_current flips to 0.  FTS is a search
        # index for active recall; superseded nodes remain in the graph and are
        # reachable via graph traversal and engram_list, but should not surface in
        # text search results.
        #
        # The extra AND COALESCE(NEW.status, '') != 'retracted' prevents a
        # double-delete when retraction does a combined UPDATE with both
        # status='retracted' and is_current=0 in one statement: without the guard
        # both this trigger and nodes_retract_remove_from_fts would fire, and
        # the second FTS 'delete' raises "database disk image is malformed".
        """
        CREATE TRIGGER IF NOT EXISTS nodes_fts_supersede_remove
        AFTER UPDATE OF is_current ON nodes
        WHEN NEW.is_current = 0 AND OLD.is_current = 1
          AND COALESCE(NEW.status, '') != 'retracted'
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', OLD.rowid,
                    COALESCE(OLD.claim, ''),
                    COALESCE(OLD.quoted_text, ''),
                    COALESCE(OLD.interpretation, ''),
                    COALESCE(OLD.reported_state, ''),
                    COALESCE(OLD.trigger_text, ''));
        END
        """,
    ]:
        try:
            conn.execute(trigger_sql)
        except sqlite3.OperationalError:
            pass

    # One-shot migration (#274): remove existing retracted nodes from the FTS
    # index. Pre-existing retracted nodes were indexed before the trigger above
    # existed and were never cleaned up.
    #
    # IMPORTANT: FTS5 'delete' magic-insert is NOT idempotent — attempting to
    # delete a rowid that's already absent from the index raises
    # "database disk image is malformed". We gate this migration with
    # PRAGMA user_version so it runs exactly once (user_version 0 → 1).
    # The trigger above handles all retractions going forward; this migration
    # is only needed to clean up the pre-trigger state on upgrade.
    # Superseded nodes are cleaned up by migration 3 below.
    #
    # Note: nodes_fts uses content='nodes' (external-content FTS5) with
    # deliberate index-side deletion of retracted rows.  As a result, FTS5's
    # 'integrity-check' command always reports "malformed" on a graph that has
    # retractions — this is expected by design, not a sign of corruption (#781).
    current_user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_user_version < 1:
        try:
            conn.execute(
                """
                INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
                SELECT 'delete', rowid,
                       COALESCE(claim, ''),
                       COALESCE(quoted_text, ''),
                       COALESCE(interpretation, '')
                FROM nodes
                WHERE status = 'retracted'
                """
            )
            # Bump version INSIDE the try block so that if the INSERT fails
            # (DatabaseError / OperationalError caught below), the gate stays at
            # 0 and the migration can retry on the next startup rather than being
            # permanently skipped.
            conn.execute("PRAGMA user_version = 1")
        except sqlite3.DatabaseError:
            # Covers both sqlite3.OperationalError (nodes_fts not yet created —
            # leave version at 0 for retry) and sqlite3.DatabaseError proper
            # (FTS5 phantom-malformed raised when 'delete'-inserting against an
            # empty index on a restore-from-dump path — #781).
            # OperationalError is a subclass of DatabaseError so the single
            # except handles both cases with unchanged retry-gate semantics.
            pass

    # Migration version 2 (#403): expand nodes_fts to cover reported_state
    # and trigger_text — feeling_report nodes were invisible to engram_query
    # because these fields weren't in the FTS index.
    #
    # Strategy: drop vocab↔fts dependency chain, rebuild both, backfill all
    # non-retracted nodes into the new 5-column schema, recreate triggers.
    # Gated by user_version so it runs exactly once.
    if current_user_version < 2:
        try:
            # Drop in dependency order: vocab depends on FTS, FTS is the target.
            conn.execute("DROP TABLE IF EXISTS nodes_fts_vocab")
            conn.execute("DROP TABLE IF EXISTS nodes_fts")
            # Drop old triggers so they can be recreated with new column list.
            conn.execute("DROP TRIGGER IF EXISTS nodes_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS nodes_fts_delete")
            conn.execute("DROP TRIGGER IF EXISTS nodes_retract_remove_from_fts")
            # Recreate FTS table with 5-column schema.
            conn.execute(
                """
                CREATE VIRTUAL TABLE nodes_fts USING fts5(
                    claim, quoted_text, interpretation, reported_state, trigger_text,
                    content='nodes', content_rowid='rowid'
                )
                """
            )
            # Recreate the three triggers with updated column lists.
            conn.execute(
                """
                CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
                    INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
                    VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation, new.reported_state, new.trigger_text);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER nodes_fts_delete AFTER DELETE ON nodes BEGIN
                    INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
                    VALUES ('delete', old.rowid, old.claim, old.quoted_text, old.interpretation, old.reported_state, old.trigger_text);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER nodes_retract_remove_from_fts
                AFTER UPDATE OF status ON nodes
                WHEN NEW.status = 'retracted' AND COALESCE(OLD.status, '') != 'retracted'
                  AND OLD.is_current = 1
                BEGIN
                    INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
                    VALUES ('delete', OLD.rowid,
                            COALESCE(OLD.claim, ''),
                            COALESCE(OLD.quoted_text, ''),
                            COALESCE(OLD.interpretation, ''),
                            COALESCE(OLD.reported_state, ''),
                            COALESCE(OLD.trigger_text, ''));
                END
                """
            )
            # Backfill all non-retracted nodes into the new 5-column schema.
            conn.execute(
                """
                INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
                SELECT rowid,
                       COALESCE(claim, ''),
                       COALESCE(quoted_text, ''),
                       COALESCE(interpretation, ''),
                       COALESCE(reported_state, ''),
                       COALESCE(trigger_text, '')
                FROM nodes
                WHERE status != 'retracted'
                """
            )
            # Recreate vocab table (depends on nodes_fts; must come after FTS rebuild).
            try:
                from engram_idf import ensure_vocab_table
                ensure_vocab_table(conn)
            except Exception:
                pass
            conn.execute("PRAGMA user_version = 2")
        except sqlite3.DatabaseError:
            pass

    # Migration version 3 (#195, design rev 2026-07-02): remove superseded nodes
    # from the FTS search index.  FTS is a live-recall index; superseded nodes
    # belong in the graph (traversable via edges and engram_list) but must not
    # pollute text search results or IDF term-frequency counts.
    #
    # Two steps:
    #   a. Backfill: delete all superseded (is_current=0, non-retracted) nodes
    #      from nodes_fts.  Retracted nodes were already removed by migration 1.
    #   b. Trigger: nodes_fts_supersede_remove is created at setup time (above)
    #      via CREATE TRIGGER IF NOT EXISTS; the CREATE here is a no-op safety net
    #      for the path where setup ran before this migration block was added.
    if current_user_version < 3:
        try:
            conn.execute(
                """
                INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation,
                                      reported_state, trigger_text)
                SELECT 'delete', rowid,
                       COALESCE(claim, ''),
                       COALESCE(quoted_text, ''),
                       COALESCE(interpretation, ''),
                       COALESCE(reported_state, ''),
                       COALESCE(trigger_text, '')
                FROM nodes
                WHERE is_current = 0 AND status != 'retracted'
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS nodes_fts_supersede_remove
                AFTER UPDATE OF is_current ON nodes
                WHEN NEW.is_current = 0 AND OLD.is_current = 1
                  AND COALESCE(NEW.status, '') != 'retracted'
                BEGIN
                    INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation,
                                          reported_state, trigger_text)
                    VALUES ('delete', OLD.rowid,
                            COALESCE(OLD.claim, ''),
                            COALESCE(OLD.quoted_text, ''),
                            COALESCE(OLD.interpretation, ''),
                            COALESCE(OLD.reported_state, ''),
                            COALESCE(OLD.trigger_text, ''));
                END
                """
            )
            conn.execute("PRAGMA user_version = 3")
        except sqlite3.DatabaseError:
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

    # One-time backfill: write resolved_by for resolved nodes where it is NULL.
    # Affects pre-#759 contradictions and any other resolvable nodes that were
    # resolved before the resolved_by column was written. Idempotent — guarded
    # by the NULL check inside _backfill_resolved_by (skips immediately if 0 rows).
    _backfill_resolved_by(conn)

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


def _get_db() -> sqlite3.Connection:
    """Get a database connection, creating tables if needed.

    Fails LOUD if the DB file is missing or seed-empty — the plugin packaging
    has no installer-script precondition, so missing DB means engram-first-session
    needs to bootstrap (which it does via bootstrap.py, called from the skill).
    Per-call check is intentionally lazy: subsequent calls after bootstrap.py
    runs in the same session succeed without MCP restart (no /mcp dance).

    Guard-bypass env vars (set when running in a controlled environment that
    doesn't need the guards):
      - ENGRAM_BOOTSTRAP=1 — bootstrap.py sets this in its own process before
        calling engram_add_axiom / engram_add_definition / engram_add_goal to
        seed the graph; that call chain goes through _get_db(), so without the
        bypass the fail-loud guards block the very seeding that's supposed to
        satisfy them (chicken-and-egg).
      - ENGRAM_NO_DB_GUARDS=1 — test-mode bypass (matches ENGRAM_NO_EMBEDDINGS
        / ENGRAM_NO_POLARITY precedent). The test fixtures monkey-patch DATA_DIR
        and DB_PATH to a tempdir, then call tool handlers whose first _get_db()
        opens-and-creates the DB; the fail-loud would block the legitimate
        test-fixture setup. conftest.py sets this once at session start.

    Both are scoped to the env that sets them (bootstrap subprocess + pytest
    session). The production MCP server never sets either, so guards stay
    active for real user flows.
    """
    bootstrap_mode = (
        os.environ.get("ENGRAM_BOOTSTRAP") == "1"
        or os.environ.get("ENGRAM_NO_DB_GUARDS") == "1"
    )
    if not DB_PATH.exists() and not bootstrap_mode:
        raise RuntimeError(_db_missing_message())
    _ensure_data_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # #1672 (blueprint H6): conservative SQLite defaults measured in effect
    # (synchronous=FULL, mmap_size=0, cache_size~2MB, temp_store=file). Each
    # change below is faster-never-looser: it does not weaken graph
    # consistency (no corruption risk), only the durability window of the
    # most recent commit(s) -- acceptable for a git-backed, git-snapshotted
    # substrate.
    #
    # synchronous=NORMAL (from FULL): with journal_mode=WAL (already set
    # above), NORMAL still fsyncs at every WAL checkpoint, so the WAL itself
    # can't be corrupted by a crash of this process. Per SQLite's own docs
    # (pragma.html#pragma_synchronous), a WAL+NORMAL transaction can still
    # roll back the last few committed-since-checkpoint transactions on a
    # power loss OR an OS/kernel-level crash -- narrower than FULL's
    # durability, but never a corruption risk. SQLite's own docs recommend
    # NORMAL as the standard pairing with WAL for exactly this reason.
    conn.execute("PRAGMA synchronous=NORMAL")
    # mmap_size (from 0): memory-map the DB file for reads, cutting a
    # read()-syscall + copy per page. Read-only optimization -- no effect on
    # write durability or consistency guarantees.
    conn.execute("PRAGMA mmap_size=268435456")  # 256MB
    # cache_size (from SQLite's ~2MB default, -2000): larger page cache
    # reduces disk I/O for repeated reads within a connection's lifetime.
    # Negative value = size in KiB (SQLite convention), not a page count.
    conn.execute("PRAGMA cache_size=-64000")  # ~64MB
    # temp_store=MEMORY (from file): temp b-trees/sort spills (e.g. ORDER BY,
    # CREATE INDEX) use RAM instead of disk temp files. No persistence
    # implication -- temp storage is never durable in either mode.
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    _load_vec_extension(conn)

    _assert_sqlite_version(conn)

    # #1669: run the backfill/migration/DAG-check block once per resolved
    # DB path per process (guard-after-success; see _db_setup_done_paths).
    #
    # The path-keyed set alone is NOT sufficient: some callers (notably test
    # fixtures — e.g. tests/test_engram_add_edge.py's fresh_server(), which
    # shutil.rmtree()s and recreates the SAME tempdir across many tests in
    # one process) delete and recreate the DB file at an already-"done"
    # path, which would otherwise leave a brand-new, table-less DB file
    # wrongly skipped. So a "done" path is also cheaply re-verified via a
    # single sqlite_master lookup for the `nodes` table (an indexed catalog
    # lookup — negligible next to the backfill/DAG-check block it guards)
    # before trusting the guard; a miss re-runs the full one-time setup.
    # .resolve() so symlink/relative aliases of the same physical DB file
    # share one guard key (reviewer-fairy suggestion, PR #1678). Worst case
    # without it was a redundant idempotent setup run, not corruption.
    resolved_db_path = str(DB_PATH.resolve())

    def _schema_present() -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone() is not None
        except sqlite3.Error:
            return False

    if resolved_db_path not in _db_setup_done_paths or not _schema_present():
        with _db_setup_lock:
            if resolved_db_path not in _db_setup_done_paths or not _schema_present():
                _run_db_one_time_setup(conn)
                _db_setup_done_paths.add(resolved_db_path)

    # Empty-graph sentinel — DB exists but bootstrap.py never seeded it.
    # Distinct from the DB-missing case at the top: this happens if bootstrap
    # crashed mid-run, or the user created an empty knowledge.db manually.
    # Same fail-loud path; engram-first-session handles both. Bypass under
    # ENGRAM_BOOTSTRAP=1 so bootstrap.py's own seeding can run (its first
    # axiom-add lands on an empty graph by definition).
    #
    # Knowledge-pack exception: engram-package DBs legitimately have no seed
    # axioms — they are curated observation/derivation sets, not agent brains.
    # config.json sets is_knowledge_pack=true at pack-init time (engram-pkg
    # init writes it before the first _get_db() call), so this guard fires
    # only on personal-install DBs where the empty state means bootstrap crash.
    if not bootstrap_mode:
        is_knowledge_pack = False
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            is_knowledge_pack = bool(cfg.get("is_knowledge_pack", False))
        except Exception:
            pass
        if not is_knowledge_pack:
            seed_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='axiom'"
            ).fetchone()[0]
            if seed_count == 0:
                conn.close()
                raise RuntimeError(_seed_missing_message())

    # ── WAL-guard checks (#786) ─────────────────────────────────────────────
    # Skipped in test/bootstrap mode (ENGRAM_NO_DB_GUARDS=1 / ENGRAM_BOOTSTRAP=1)
    # to prevent false positives from the test framework's temp-dir lifecycle
    # (multiple tests reuse the same path, causing lingering fds to appear deleted).
    # Production MCP servers never set these flags.
    if not bootstrap_mode:
        global _walguard_startup_done
        if not _walguard_startup_done:
            _walguard_startup_done = True
            _walguard_startup_clear(conn)

        # Throttled (≥30 s interval): detect shm displacement, emergency-dump
        # if detected, write degraded marker.  Must not raise.
        _run_walguard_check(conn)
    else:
        # Emit one stderr line on the FIRST skipped check so that a production
        # server accidentally started with ENGRAM_NO_DB_GUARDS=1 is visible
        # rather than silently unguarded.
        global _walguard_disabled_logged
        if not _walguard_disabled_logged:
            _walguard_disabled_logged = True
            import sys as _sys
            print(
                "[walguard] disabled by ENGRAM_NO_DB_GUARDS / bootstrap mode (#786)",
                file=_sys.stderr,
            )

    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _humanized_ago(then_iso: Optional[str], now: Optional[datetime] = None) -> str:
    """Render an ISO-8601 timestamp as a humanized relative string.

    MECH-2 of the time-awareness design (the time-awareness derivation): converts buried
    created_at / focused_at / last_assessed_at signals into surfaced ones
    alongside the ISO timestamp. Scale: seconds → years, more precision at
    the recent end. Returns '?' when input is missing, 'parse-error' on
    malformed ISO, 'future?' if the timestamp is ahead of now. Always
    additive — never replaces the ISO field it accompanies (the time-awareness derivation MECH-2
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
            "tier2_max_nodes": 1000,
        })
    return {"decay_base": 1.014, "current_turn": 0,
            "tier2_max_nodes": 1000}


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


def _git_sha_for_file(path: Path) -> Optional[str]:
    """Return the git SHA of a file's HEAD content, or 'dirty' if uncommitted.

    Used to fingerprint CLAUDE.md at feeling-report write time.
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

    Tier 1 (working memory): RETIRED — tier1_max_nodes config key removed.
                             Callers use tier=2 instead.
    Tier 2 (queryable): used by reflect, checkpoint, stats, query, add_* similarity-hints
    Tier 3 (total): no threshold, everything visible
    """
    if tier >= 3:
        return 0.0  # no filter

    mem = _get_memory_config()
    # Tier 1 is retired; fall through to tier-2 handling for any legacy callers.
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


def _compute_recall_set_continuity(conn: sqlite3.Connection, checkpoint_mode: str) -> dict:
    """Compute the Recall-Set Continuity metric (v1) for the current checkpoint.

    Recall-Set Continuity = Jaccard similarity J(A,B) = |A∩B| / |A∪B| between
    the active-recallable (tier1+tier2 / "searchable") node-ID set at this
    checkpoint (B, the "current" set) and the set stored at the
    immediately-prior diagnostic_history row (A, the "prior" set). Tier1 is
    a verified strict subset of tier2 (see _get_tier_threshold — tier1's
    percentile threshold is always >= tier2's, i.e. more selective), so
    "tier1+tier2 membership" reduces to just the tier2-threshold set; no
    separate tier1 query or literal set-union is needed.

    Called from BOTH the engram_nap wrapper AND the engram_advance_turn
    (sleep) wrapper (server.py), at every diagnostic_history checkpoint —
    NOT from engram_diagnose() itself. engram_diagnose() is its own
    directly-callable MCP tool ("Use any time" per its docstring), not
    checkpoint-only, so computing and injecting this here (rather than
    inside _diagnose_impl) keeps ad-hoc engram_diagnose() calls from
    carrying a tier2_max_nodes-sized (default 1000) node-ID list on every
    invocation.

    Cadence decision (Borges, the metric's designer, 2026-07-02): the
    original design intent said "every nap," which the first implementation
    took literally (nap-only). That was an accidental scope gap, not the
    intent — "every nap" was shorthand for "every checkpoint." The sleep
    boundary is in fact the SINGLE MOST MEANINGFUL measurement point:
    turn-advance triggers forgetting (decay -> tier demotion) PLUS
    consolidation (retract/supersede/dream), so the Jaccard ACROSS sleep
    directly answers whether a day's consolidation preserved mental
    continuity — exactly the question the metric exists to watch. Excluding
    it would blind the metric to its own primary use case, and it would
    also break the timeseries daily (every nap immediately following a
    sleep would show a spurious jaccard=None). Hence: compute at every
    checkpoint, nap and sleep alike.

    `checkpoint_mode` ("nap" or "advance_turn") is stored alongside the
    result so downstream readers (health-alerting, the paper) can
    distinguish the two cases: a big J drop AT a sleep checkpoint is
    expected and meaningful (consolidation reshaping); a big J drop AT a
    plain nap checkpoint is the actual alarm (unexpected continuity
    disruption within a day, no consolidation to explain it). The prior
    row's mode is also surfaced (`prior_checkpoint_mode`) for the same
    reason — a nap-to-nap comparison and a sleep-to-nap comparison carry
    different interpretive weight even when both land on a "nap" current
    checkpoint.

    Returns a dict meant to be stored verbatim under
    metrics["recall_set_continuity"] in the new diagnostic_history row:
        {
            "checkpoint_mode": str,           # "nap" or "session" (engram_advance_turn) — the
                                               # literal mode string _checkpoint_internal uses
            "tier2_ids": [...],               # current tier2 (searchable) node-ID set
            "set_size": int,
            "jaccard": float | None,          # None only when undefined (see reason)
            "prior_snapshot_turn": int | None,
            "prior_checkpoint_mode": str | None,  # mode of the row diffed against
            "reason": str | None,             # populated iff jaccard is None
        }

    Jaccard undefined cases (jaccard is None, never a fabricated 0.0/1.0):
      - No prior diagnostic_history row exists at all (first-ever checkpoint).
      - A prior row exists but predates this feature (no
        recall_set_continuity.tier2_ids key in its metrics blob) — a
        one-time transition cost for rows written before this metric (or
        before the sleep-cadence fix) shipped. Once every checkpoint writes
        the key, the immediately-prior row always has it, so this is not an
        ongoing steady-state gap — we deliberately do NOT search further
        back than one row for an older comparable snapshot (that would
        silently widen the comparison window in a way the "consecutive
        checkpoints" framing doesn't intend; a fresh None here is more
        honest than a stale multi-checkpoint-old comparison presented as if
        it were "the prior").

    Zero-division convention: if BOTH the current and prior sets are empty
    (|A ∪ B| = 0), jaccard is defined as 1.0 — two empty recall-sets are
    treated as trivially/maximally continuous (the common Jaccard convention
    for the degenerate empty/empty case), rather than surfacing None. None
    is reserved for "there is no prior set to compare against" — a distinct
    condition from "the sets we DO have happen to both be empty."

    Storage pruning (Borges, colleague review on #1632): `tier2_ids` is
    ONLY ever read from the immediately-prior row (`ORDER BY id DESC LIMIT
    1`) — historical rows' tier2_ids are never re-read, only their
    already-computed `jaccard` is (for the timeseries). Left unpruned,
    diagnostic_history would carry a full up-to-tier2_max_nodes ID list in
    EVERY row forever (~15-20KB/row at default tier2_max_nodes=1000, and
    the table has no pruning). So once THIS checkpoint has diffed against
    the prior row, we strip `tier2_ids` from that prior row's stored
    metrics blob (in place, via a targeted UPDATE) — only the newest row
    ever carries the full ID list at rest. The prior row's `jaccard`,
    `checkpoint_mode`, etc. are left untouched (still needed for the
    timeseries); only the now-superfluous ID list is dropped.
    """
    tier2_threshold = _get_tier_threshold(conn, 2)
    current_ids = {
        row["id"] for row in conn.execute(
            "SELECT id FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (tier2_threshold,),
        ).fetchall()
    }

    prior_row = conn.execute(
        "SELECT id, turn, metrics FROM diagnostic_history ORDER BY id DESC LIMIT 1"
    ).fetchone()

    result: dict = {
        "checkpoint_mode": checkpoint_mode,
        "tier2_ids": sorted(current_ids),
        "set_size": len(current_ids),
        "jaccard": None,
        "prior_snapshot_turn": None,
        "prior_checkpoint_mode": None,
        "reason": None,
    }

    if prior_row is None:
        result["reason"] = "no prior diagnostic_history row — first checkpoint ever"
        return result

    try:
        prior_metrics = json.loads(prior_row["metrics"])
    except (TypeError, ValueError):
        result["reason"] = (
            "prior diagnostic_history row's metrics blob was not parseable JSON"
        )
        return result

    prior_rsc = prior_metrics.get("recall_set_continuity") if isinstance(prior_metrics, dict) else None
    if not isinstance(prior_rsc, dict) or "tier2_ids" not in prior_rsc:
        result["reason"] = (
            "prior diagnostic_history row has no comparable tier2_ids — either "
            "predates the Recall-Set Continuity metric, or predates the "
            "sleep-cadence fix (was written by a checkpoint type that didn't "
            "compute this metric yet), or its tier2_ids were already pruned "
            "after a later checkpoint diffed against it"
        )
        result["prior_snapshot_turn"] = prior_row["turn"]
        return result

    prior_ids = set(prior_rsc["tier2_ids"])
    result["prior_snapshot_turn"] = prior_row["turn"]
    result["prior_checkpoint_mode"] = prior_rsc.get("checkpoint_mode")

    union_size = len(current_ids | prior_ids)
    if union_size == 0:
        result["jaccard"] = 1.0
    else:
        intersection_size = len(current_ids & prior_ids)
        result["jaccard"] = round(intersection_size / union_size, 6)

    # Prune the prior row's now-superfluous tier2_ids in place — it has
    # served its one purpose (this diff) and will never be read again.
    # Best-effort: pruning failure must never break the checkpoint itself.
    try:
        prior_rsc_pruned = dict(prior_rsc)
        prior_rsc_pruned.pop("tier2_ids", None)
        prior_metrics["recall_set_continuity"] = prior_rsc_pruned
        conn.execute(
            "UPDATE diagnostic_history SET metrics = ? WHERE id = ?",
            (json.dumps(prior_metrics), prior_row["id"]),
        )
    except Exception:
        pass

    return result


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


DEDUP_TOP_K = 15                          # was 5; the dedup-top-k-floor derivation at sim 0.75 to the dedup-near-miss observation was cut by top_k floor, not similarity floor


DEDUP_MIN_SIMILARITY = 0.40               # was 0.50; tight-group recall lifts 65% → 86% at this threshold (precision still 83%)


ACTION_HINT_CORROBORATE_THRESHOLD = 0.65  # was 0.80; old tier fired on only 9.5% of true relateds (effectively dead code)


ACTION_HINT_RELATED_THRESHOLD = 0.50      # was 0.60; recall-precision shift consistent with v4 F1 peak


POLARITY_DEFAULT_MODEL = "dleemiller/ModernCE-large-nli"


POLARITY_DEFAULT_THRESHOLD = 0.46


POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK = 0.30  # below this cosine, skip NLI (truly unrelated)


def _get_thresholds_config() -> dict:
    """Get similarity-threshold config (config.json `thresholds` section).

    Returns a dict with the keys defined in tools/migration/migrate_config_v3.py
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
                # Show predictions that depend on this observation (incoming
                # supported_by; post-direction-fix, observations are targets of
                # supported_by edges sourced from the predictions they ground).
                edges = conn.execute(
                    "SELECT source_id FROM edges WHERE target_id = ? AND relation = 'supported_by' "
                    "AND source_id IN (SELECT id FROM nodes WHERE type = 'prediction')",
                    (n["id"],),
                ).fetchall()
                for e in edges:
                    lines.append(f"Depended on by: {e['source_id']}")
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


def _write_binary_backup(
    src_db_path: str,
    backup_dir: Path,
    max_keep: int = 7,
) -> dict:
    """Write a timestamped binary hot-copy of knowledge.db to backup_dir.

    Uses sqlite3.Connection.backup() — the same WAL-safe hot-copy approach as
    engram_backup.dump_stripped — so the live database is never locked hard.

    Naming: knowledge-YYYYMMDD.db (one file per calendar day). Idempotent: if a
    file for today already exists, the write is skipped.

    Rotation: after a successful write, all but the max_keep newest files (sorted
    by mtime) are deleted.

    Returns:
        {"path": str, "bytes": int, "pruned": int}  — successful write
        {"skipped": "same-day copy exists", "path": str}  — idempotent skip
        {"error": str}  — any failure (best-effort: never raises, never blocks nap)

    Note: backup_dir (DATA_DIR / "db-backup") should be listed in
    ~/.engram/.gitignore so the binary files are never committed to the ENGRAM
    git repo. The gitignore for the ENGRAM data dir lives at runtime
    (~/.engram/.gitignore), not in the source tree.
    """
    dest_path: Path | None = None
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        today_str = date.today().strftime("%Y%m%d")
        dest_path = backup_dir / f"knowledge-{today_str}.db"

        if dest_path.exists():
            return {"skipped": "same-day copy exists", "path": str(dest_path)}

        src_conn = sqlite3.connect(src_db_path)
        try:
            dest_conn = sqlite3.connect(str(dest_path))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()

        backup_bytes = dest_path.stat().st_size

        # Prune: keep only the max_keep newest files by mtime.
        all_backups = sorted(
            backup_dir.glob("knowledge-*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        pruned = 0
        for old_file in all_backups[max_keep:]:
            try:
                old_file.unlink()
                pruned += 1
            except OSError:
                pass  # best-effort: skip files we can't remove

        return {"path": str(dest_path), "bytes": backup_bytes, "pruned": pruned}

    except Exception as e:
        # Remove any partial/zero-byte dest_path so a same-day retry isn't
        # blocked by the idempotency check pointing at a corrupt file.
        if dest_path is not None:
            try:
                dest_path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"error": str(e)}


def _commit_snapshot(
    conn: sqlite3.Connection,
    message: str,
    mode: str,
    skip_checkpoint: bool = False,
) -> dict:
    """Durable snapshot dispatch (#1673).

    Normal nap/session checkpoints take the ASYNC path: a per-turn fsync'd binary
    snapshot is captured synchronously (the durability boundary) and the slow
    iterdump→knowledge.sql + git commit/push is enqueued to the serialized snapshot
    worker, moving the ~87% turn-advance cost off the hot path.

    The delicate cases stay on the proven SYNCHRONOUS path (_commit_snapshot_sync):
      - mode == "emergency": the walguard #786 dump must secure data inline before
        returning — an enqueue-and-return would defeat the point.
      - a degraded WAL marker is present: we're mid-corruption-handling; keep the
        careful sync ordering unchanged.
      - the async worker can't run (git unavailable / import or start failure) or
        the sync binary capture fails: fall back to sync so no turn loses durability.
    """
    degraded = False
    try:
        import engram_walguard as _wg
        degraded = _wg.read_degraded_marker(DATA_DIR) is not None
    except Exception:
        pass  # can't read marker → conservative: treat as not-degraded, dispatch normally

    if mode != "emergency" and not degraded:
        async_result = _commit_snapshot_async(conn, message, mode, skip_checkpoint)
        if async_result is not None:
            return async_result
        # async unavailable or capture failed → fall through; durability preserved sync.

    result = _commit_snapshot_sync(conn, message, mode, skip_checkpoint)
    # R3 on the sync path too: an emergency/degraded/fallback checkpoint should still
    # surface the async worker's backlog loudly (a git history that silently stops
    # advancing is a false-in-the-graph failure). Best-effort; never blocks the commit.
    try:
        import engram_snapshot_worker as _sw
        if isinstance(result, dict):
            result.setdefault("snapshot_lag", _sw.compute_snapshot_lag())
    except Exception:
        pass
    return result


def _commit_snapshot_async(
    conn: sqlite3.Connection,
    message: str,
    mode: str,
    skip_checkpoint: bool,
) -> Optional[dict]:
    """#1673 async path. Returns the result dict on success, or None to signal the
    caller to fall back to the synchronous path (git unavailable / worker cannot
    run / the sync binary capture failed — never silently drop a turn's durability).

    SYNC work here (the durability boundary, before returning):
      1. #786 WAL read-mark on conn.
      2. Per-turn fsync'd immutable binary snapshot (R1/R2), keyed (turn, seq).
      3. Release read-mark; WAL RESTART checkpoint (unchanged skip rules).
      4. Daily coarse binary ladder (_write_binary_backup), unchanged.
      5. Durably enqueue + fsync the async commit job; return only after the fsync.
    The worker then regenerates knowledge.sql + graph_snapshot.md from the captured
    immutable snapshot and commits/pushes them (R5: it never touches the live WAL).
    """
    global _git_available

    # The worker's job is a git commit; without git it can't make progress, so let
    # the sync path (which reports git-unavailable) handle that case.
    if not _git_available:
        _init_git()
    if not _git_available:
        return None

    try:
        import engram_snapshot_worker as _sw
        _sw.start_worker()  # idempotent: seeds seq, clears stale lock, replays, starts thread
    except Exception as e:
        print(f"[engram] snapshot worker unavailable, using sync commit: {e}", file=sys.stderr)
        return None

    turn = _get_current_turn()

    # 1. #786 read-mark on conn — a real-page read forces a genuine WAL read mark so
    #    the capture sees a consistent committed view (identical discipline to the
    #    sync dump; verified conn.backup() works while this txn is open). Guard: don't
    #    BEGIN if conn already has an open transaction.
    _held_read_txn = False
    if not conn.in_transaction:
        try:
            conn.execute("BEGIN DEFERRED")
            _held_read_txn = True
            conn.execute("SELECT count(*) FROM sqlite_master")
        except Exception:
            pass  # best-effort; capture still runs

    # 2. Per-turn durable binary snapshot (R1/R2) under the read-mark.
    seq = _sw.next_seq()
    capture = _sw.write_durable_snapshot(conn, turn, seq)

    # 3a. Release the read-mark before the RESTART checkpoint (RESTART needs all
    #     WAL readers gone).
    if _held_read_txn:
        try:
            conn.execute("COMMIT")
        except Exception:
            pass

    # If the sync capture failed, we have NOT secured this turn — fall back to sync
    # so durability is not silently dropped.
    if capture.get("error"):
        return None

    # 3b. WAL RESTART checkpoint (same skip rules; no sync dump ⇒ no _dump_error gate).
    wal_warning = None
    if not skip_checkpoint:
        try:
            conn.execute("PRAGMA wal_checkpoint(RESTART)")
        except Exception as e:
            wal_warning = f"wal_checkpoint failed: {e}"

    # 4. Daily coarse binary ladder (best-effort, unchanged) — retained alongside the
    #    per-turn snapshots for the coarse 7-day restore window.
    _db_backup_result = _write_binary_backup(str(DB_PATH), DATA_DIR / "db-backup")

    # 5. Durably enqueue the async commit job (fsync'd inside enqueue_job) — the tool
    #    returns only after this, honoring the "return after enqueue + fsync" guarantee.
    _sw.enqueue_job(turn, seq, message, mode)

    result = {
        "async": True,
        # git_committed is intentionally None here: the commit is pending in the
        # worker. The only programmatic reader of git_committed is the walguard
        # emergency path, which always takes the sync branch.
        "git_committed": None,
        "enqueued_turn": turn,
        "enqueued_seq": seq,
        "binary_backup": capture,
        "db_backup": _db_backup_result,
        "snapshot_lag": _sw.compute_snapshot_lag(),
    }
    if wal_warning:
        result["wal_warning"] = wal_warning
    return result


def _commit_snapshot_sync(
    conn: sqlite3.Connection,
    message: str,
    mode: str,
    skip_checkpoint: bool = False,
) -> dict:
    """Synchronous snapshot + git commit (the pre-#1673 path, kept as the emergency
    and fallback carrier). Generate the markdown snapshot, write it, and commit ~/.engram/.git.

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

    Ordering (#786 fix): dump_stripped runs BEFORE the WAL checkpoint.
    Connection.backup() (used inside dump_stripped) reads the live
    main-file+WAL merged state — the dump already captures all committed
    writes without a prior checkpoint.  The RESTART checkpoint runs AFTER
    the dump so the WAL is checkpointed and its write-position reset for the
    next nap cycle.

    Additionally, conn holds an active WAL read transaction across the dump
    (#786 Option B): this prevents the temporary ``src`` connection opened
    inside dump_stripped from ever being seen as the last WAL reader, which
    would let SQLite unlink the -shm file while the server still has it
    mmap'd.  The read mark is released before the RESTART checkpoint
    (RESTART needs all readers gone to reset the WAL write-position).

    If dump_stripped raises, the checkpoint does NOT run for that nap.
    This is intentional: a failed backup skipping the WAL checkpoint is
    conservative — the WAL carries to the next nap, where the backup will
    be retried.

    skip_checkpoint=True or a degraded marker present: checkpoint is
    omitted entirely (the #786 degraded-state override is unchanged).

    Args:
        conn: Live database connection.
        message: Commit message body.
        mode: Commit mode tag (e.g. "nap", "sleep", "emergency").
        skip_checkpoint: When True, omit the PRAGMA wal_checkpoint(RESTART)
            step.  Forced True when a degraded marker is present (regardless of
            caller), to prevent checkpointing under a split-brain WAL-index.
    """
    global _git_available

    # ── Degraded-marker override (#786) ────────────────────────────────────
    # If a degraded marker is present, force skip_checkpoint=True regardless of
    # caller.  Checkpointing under a displaced WAL-index is the corruption vector
    # (#786); the SQL dump reads through the coherent connection and is safe.
    if not skip_checkpoint:
        try:
            import engram_walguard as _wg
            if _wg.read_degraded_marker(DATA_DIR) is not None:
                skip_checkpoint = True
        except Exception:
            pass  # failure to read marker → conservative: proceed with caller's value

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

    # 2. Generate embedding-stripped SQL text dump (the SQL-dump-replaces-binary derivation).
    #     Embeddings (~384-float blobs, ~37 MB) are regenerable from claim text and must
    #     NOT be included — they churn on every nap and defeat delta compression, growing
    #     .git to GBs (verified 2026-06-02: 79.7 MB → 42.6 MB stripped, lossless rebuild).
    #     Method: engram_backup.dump_stripped (pure-Python, no sqlite3 CLI).
    #
    #     Ordering (#786 fix — Option A): dump runs BEFORE wal_checkpoint(RESTART).
    #     Connection.backup() reads the live main-file+WAL merged view — no prior
    #     checkpoint is needed for backup correctness.  The RESTART runs AFTER so
    #     the WAL is compacted without racing the dump's secondary connection.
    #
    #     Option B — WAL read-mark held across the dump: acquire a WAL read
    #     transaction on conn before calling dump_stripped so that conn is always
    #     a WAL reader while the temporary ``src`` connection inside dump_stripped
    #     is open.  This prevents SQLite from treating ``src`` as the last WAL
    #     reader when src.close() fires — which would let it unlink the -shm file
    #     while the server still has it mmap'd → "database disk image is malformed"
    #     (the Ariadne 06-03 + Luria 06-17 incident, #786).
    #
    #     The read mark must be released before RESTART: a RESTART checkpoint
    #     needs all active WAL readers gone to compact the log.  Read mark is
    #     held only for the duration of dump_stripped, then released.
    #
    #     Restore path: `sqlite3 ~/.engram/knowledge.db < knowledge.sql`
    #     After restore: run `python tools/engram-regenerate-embeddings.py` to rebuild
    #     semantic search (embedding column is NULL in the dump, NULL in the restored DB).
    sql_dump_path = DATA_DIR / "knowledge.sql"
    _dump_stats = None
    _dump_error = None

    # Option B: acquire a WAL read mark on conn before dump so conn is always a
    # WAL reader while dump_stripped's temporary src connection is live.  A
    # deferred BEGIN alone is not enough — SQLite only assigns a read mark when
    # a statement reads a real database page.  `SELECT 1` is a constant-value
    # expression that never touches any page, so it acquires NO read mark and a
    # concurrent `wal_checkpoint(RESTART)` from another connection will not see
    # this connection as a reader (verified empirically: RESTART returns busy=0
    # with SELECT 1 vs busy=1 with a real-page read such as sqlite_master).
    # We use `SELECT count(*) FROM sqlite_master`: sqlite_master is page 1,
    # always present, always readable, and forces a genuine WAL read mark.
    # Guard: don't issue BEGIN if conn already has an open transaction
    # (sqlite3 default isolation_level is non-autocommit — implicit BEGIN fires
    # on first DML, so in_transaction is True after any uncommitted write).
    _held_read_txn = False
    if not conn.in_transaction:
        try:
            conn.execute("BEGIN DEFERRED")
            # Set immediately after BEGIN so the finally-COMMIT always fires,
            # even if the read statement below raises.  BEGIN opened a
            # transaction; cleanup must be airtight regardless of what follows.
            _held_read_txn = True
            conn.execute("SELECT count(*) FROM sqlite_master")
        except Exception:
            pass  # best-effort; the dump still runs; #786 race window stays small

    try:
        import engram_backup  # local import: server.py's dir is on sys.path; dodges import-order
        _dump_stats = engram_backup.dump_stripped(str(DB_PATH), str(sql_dump_path))
    except Exception as e:
        # LOUD, not silent: the class-5 failure this PR exists to prevent is a
        # backup that fails without anyone noticing. Record + log; do NOT `pass`,
        # do NOT abort the rest of the snapshot commit.
        import sys as _sys
        print(f"[engram backup] CRITICAL: knowledge.sql dump FAILED: {e}", file=_sys.stderr)
        _dump_error = str(e)
    finally:
        # Release the WAL read mark before the RESTART checkpoint below.
        # RESTART needs all WAL readers gone; holding the mark past this point
        # would silently downgrade RESTART to a partial checkpoint.
        if _held_read_txn:
            try:
                conn.execute("COMMIT")
            except Exception:
                pass

    # 2b. Binary backup: timestamped hot-copy of knowledge.db for fast point-in-time
    #     restore without re-running the full pipeline.  Best-effort (#1142): never
    #     raises, never blocks the nap.  7-day rotating window; one file per day.
    #     Note: db-backup/ should be in ~/.engram/.gitignore — binary files, not
    #     line-diff friendly.  The .gitignore lives at runtime, not in the source tree.
    _db_backup_result = _write_binary_backup(str(DB_PATH), DATA_DIR / "db-backup")

    # 3 (was 2). Flush WAL into the main db file so the next nap starts clean.
    # Runs AFTER dump_stripped — see ordering note above (#786 Option A).
    # Skipped entirely when skip_checkpoint=True (degraded-marker override or
    # caller request).  If dump_stripped raised, _dump_error is set and the
    # checkpoint is still skipped (the ``try/finally`` above always runs COMMIT
    # before we reach here, but _dump_error being set signals the failed backup;
    # conservative choice: skip WAL compaction on a failed nap backup).
    wal_warning = None
    if not skip_checkpoint and _dump_error is None:
        try:
            conn.execute("PRAGMA wal_checkpoint(RESTART)")
        except Exception as e:
            # Non-fatal — continue, but record it
            wal_warning = f"wal_checkpoint failed: {e}"
    elif not skip_checkpoint and _dump_error is not None:
        # dump failed → skip checkpoint conservatively; WAL carries to next nap.
        wal_warning = f"wal_checkpoint skipped: dump failed ({_dump_error[:120]})"

    # Collect dump fields once so every subsequent return path carries them.
    # The agent-visible response must surface sql_dump_error on ALL exit paths,
    # not only the success path — otherwise a backup failure is visible on stderr
    # but invisible to the agent reading the nap/sleep response (the class-5
    # silent-failure mode this PR eliminates).
    # Keys: sql_dump (stats from dump_stripped), sql_dump_error (if dump failed),
    #       db_backup (result from _write_binary_backup — path/bytes/pruned or skipped/error).
    _dump_fields: dict = {}
    if _dump_stats is not None:
        _dump_fields["sql_dump"] = _dump_stats
    if _dump_error is not None:
        _dump_fields["sql_dump_error"] = _dump_error
    _dump_fields["db_backup"] = _db_backup_result

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

    # Dead branch under normal operation — step above always writes graph_snapshot.md.
    # Can only fire if snapshot generation itself failed (class-5 signal).
    if not files_to_stage:
        return {"git_committed": False, "reason": "no tracked files exist", **_dump_fields}

    # Filter out gitignored paths before staging (#731).
    # `git check-ignore -- <paths>` stdout lists the ignored ones.
    # Returncode: 0 = at least one ignored (stdout lists them), 1 = none ignored, >1 = error.
    # On error, keep all files (best-effort — don't let the guard itself break the backup).
    skipped_ignored: list[str] = []
    ci_result = _git("check-ignore", "--", *files_to_stage)
    if ci_result.returncode == 0:
        # At least one path is ignored; subtract the listed ones.
        ignored_set = {p.strip() for p in ci_result.stdout.splitlines() if p.strip()}
        skipped_ignored = [p for p in files_to_stage if p in ignored_set]
        files_to_stage = [p for p in files_to_stage if p not in ignored_set]
    # returncode == 1 → none ignored, keep all; returncode > 1 → git error, keep all (best-effort)

    if skipped_ignored:
        _dump_fields["skipped_ignored"] = skipped_ignored

    if not files_to_stage:
        # All candidate files were filtered out by check-ignore, not absent from disk.
        return {"git_committed": False, "reason": "all stageable files are gitignored", **_dump_fields}

    add_result = _git("add", "--", *files_to_stage)
    if add_result.returncode != 0:
        return {
            "git_committed": False,
            "reason": f"git add failed: {add_result.stderr.strip()}",
            **_dump_fields,
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
            **_dump_fields,
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
            **_dump_fields,
        }

    # 6. Capture the new commit SHA
    sha_result = _git("rev-parse", "HEAD")
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "unknown"

    result = {
        "git_committed": True,
        "commit_sha": sha,
        "files_committed": files_to_stage,
        "commit_message": commit_msg,
        **_dump_fields,
    }
    if wal_warning:
        result["wal_warning"] = wal_warning

    # 7. Auto-push to remote (best-effort, never blocks checkpoint)
    # Use HEAD so the push targets the current branch, not hardcoded "master" (#737).
    push_result = _git("push", "origin", "HEAD")
    if push_result.returncode == 0:
        result["remote_push"] = "success"
    else:
        result["remote_push"] = f"failed: {push_result.stderr.strip()[:200]}"

    return result


_REMOVABLE_EDGE_RELATIONS = frozenset(
    r for r, c in EDGE_CLASSIFICATIONS.items() if c["removable"]
)


ERROR_INCIDENTS_PATH = str(DATA_DIR / "error_incidents.json")
CORNERSTONE_ANCHORS_PATH = str(DATA_DIR / "cornerstone_anchors.json")
PRINCIPLE_TRIGGERS_PATH = str(DATA_DIR / "principle_triggers.json")


# ---------------------------------------------------------------------------
# #872 wave-3 promotions — helpers moved to core so family modules can use them
# without importing server.py (which would violate the acyclic constraint).
# ---------------------------------------------------------------------------

# Keys stripped recursively from all @mcp.tool response dicts.
# Strips globally: embedding, parsed_metadata, confidence_history.
# Does NOT strip match_type or similarity globally — those are raw search-pipeline
# signals meaningful in engram_query(return_debug=True); stripped only from
# similar_existing sub-blocks via _strip_similar_block() at write-tool sites.
_AGENT_STRIP_KEYS: frozenset = frozenset({
    "embedding",
    "parsed_metadata",
    "confidence_history",
})


def _strip_agent_facing(obj: object) -> object:
    """Recursively remove opaque-to-agent-reasoning keys from dicts/lists.

    Applied at every @mcp.tool return boundary (post-DB-fetch, pre-json.dumps)
    to remove fields that bloat context without providing semantic value to the
    agent. The source data in the DB is never touched — only the response copy.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_agent_facing(v)
            for k, v in obj.items()
            if k not in _AGENT_STRIP_KEYS
        }
    if isinstance(obj, list):
        return [_strip_agent_facing(item) for item in obj]
    return obj


def _count_live_exemplars(conn: sqlite3.Connection, target_id: str, target_type: str) -> int:
    """Count live exemplars supporting a cornerstone or lesson.

    Live = source/target node with is_current=1.

    For lessons (target_type='lesson'):
        - Count incoming `exemplifies` edges where source.is_current=1.

    For cornerstones (target_type='cornerstone'):
        - Count incoming `exemplifies` edges where source.is_current=1, PLUS
        - Count outgoing `supported_by` edges where target.is_current=1
          (the pre-PR-#431 cornerstone exemplar wiring pattern, kept by
          Lei's 2026-05-28 design call: coexisting schemas, both counted).
    """
    exemplifies_live = conn.execute(
        """SELECT COUNT(*) AS c
           FROM edges e
           JOIN nodes n ON n.id = e.source_id
           WHERE e.target_id = ?
             AND e.relation = 'exemplifies'
             AND n.is_current = 1""",
        (target_id,),
    ).fetchone()["c"]

    if target_type != "cornerstone":
        return exemplifies_live

    supported_by_live = conn.execute(
        """SELECT COUNT(*) AS c
           FROM edges e
           JOIN nodes n ON n.id = e.target_id
           WHERE e.source_id = ?
             AND e.relation = 'supported_by'
             AND n.is_current = 1""",
        (target_id,),
    ).fetchone()["c"]

    return exemplifies_live + supported_by_live


def _current_successor(conn, node_id: str) -> "str | None":
    """Walk the superseded_by chain from node_id to the current successor.

    Returns the first is_current=1 node reached, or None when the chain
    ends without one (retracted nodes have no successor; a chain may also
    terminate on a non-current node). Cycle-guarded — a malformed chain
    returns None rather than looping.

    Promoted from engram_revision (#919/#1005 resolve guard) for shared
    use by the is_current guard family — engram_derive's contradict guard
    (#1010) consumes it too, and family→family imports are off-convention
    (shared helpers live in core).
    """
    seen = {node_id}
    row = conn.execute(
        "SELECT superseded_by FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if not row:
        return None
    nxt = row["superseded_by"]
    while nxt and nxt not in seen:
        seen.add(nxt)
        nrow = conn.execute(
            "SELECT superseded_by, is_current FROM nodes WHERE id = ?",
            (nxt,),
        ).fetchone()
        if not nrow:
            return None
        if nrow["is_current"] == 1:
            return nxt
        nxt = nrow["superseded_by"]
    return None


# ---------------------------------------------------------------------------
# Reflect-family helpers promoted for engram_lifecycle use.
# ---------------------------------------------------------------------------

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
    import json as _json
    try:
        parsed = _json.loads(kw_raw)
        return parsed if isinstance(parsed, list) else None
    except (_json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# #872 wave-4 promotions — helpers moved to core so family modules K
# (engram_epistemic) and L (engram_cornerstone) can use them without
# importing server.py (acyclic constraint).
# ---------------------------------------------------------------------------


def _semantic_search(
    conn: sqlite3.Connection,
    query_text: str,
    top_k: int = 10,
    min_similarity: float = 0.3,
    type_filter: set = None,
    importance_threshold: float = 0.0,
    include_superseded: bool = False,
) -> list[dict]:
    """Search nodes by embedding similarity.

    Fast path: sqlite-vec vec0 KNN on the vec_nodes virtual table
    (SIMD cosine distance). Joined back to nodes for metadata + filtered
    by is_current, importance_threshold, type_filter, min_similarity.
    KNN is applied before filtering, so overfetch (top_k * OVERFETCH) to
    allow post-filtering without starving the result set.

    Slow path (fallback): O(N) pure-Python cosine — only used when the
    sqlite-vec extension failed to load.

    include_superseded: when True, allow non-current (superseded) nodes to
    surface. Retracted nodes are always excluded regardless of this flag —
    they were never valid (defense-in-depth, mirrors PR #280 FTS-side filter).

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
                # When include_superseded=False: require is_current=1 (excludes
                # both superseded and retracted). When include_superseded=True:
                # allow superseded (is_current=0) but always exclude retracted —
                # retracted nodes were never valid (defense-in-depth).
                where_parts = [f"id IN ({placeholders})"]
                if not include_superseded:
                    where_parts.append("is_current = 1")
                where_parts.append("(status IS NULL OR status != 'retracted')")
                where_parts.append("COALESCE(importance_score, 0) >= ?")
                where_clause = " AND ".join(where_parts)
                meta_rows = conn.execute(
                    f"SELECT id, type, claim, confidence, quote_type, importance_score "
                    f"FROM nodes WHERE {where_clause}",
                    ids + [importance_threshold],
                ).fetchall()
                meta = {r["id"]: r for r in meta_rows}

                scored = []
                for node_id, sim in keep:
                    r = meta.get(node_id)
                    if r is None:
                        continue  # filtered out (non-current / retracted / below threshold)
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
        # When include_superseded=False: require is_current=1 (excludes both
        # superseded and retracted). When include_superseded=True: allow
        # superseded but always exclude retracted (defense-in-depth).
        if include_superseded:
            sql = """SELECT id, type, claim, confidence, is_current, embedding,
                            importance_score, quote_type
                     FROM nodes
                     WHERE (status IS NULL OR status != 'retracted')
                     AND embedding IS NOT NULL
                     AND COALESCE(importance_score, 0) >= ?"""
        else:
            sql = """SELECT id, type, claim, confidence, is_current, embedding,
                            importance_score, quote_type
                     FROM nodes
                     WHERE is_current = 1
                     AND (status IS NULL OR status != 'retracted')
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
    except Exception as e:
        logging.warning("_semantic_search failed: %s", e)
        return []


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
    except Exception as e:
        logging.warning("_similar_existing_matches failed: %s", e)
        return []
    return matches


# Keys stripped from similar_existing / similar_existing_lessons entries
# via _strip_similar_block(). These raw search-pipeline signals are opaque
# to agent reasoning for dedup decisions; action_hint encodes the actionable
# summary and is KEPT. NOT stripped globally — meaningful in
# engram_query(return_debug=True) results (eval-harness contract, §6348).
_SIMILAR_STRIP_KEYS: frozenset[str] = frozenset({
    "match_type",
    "similarity",
})


def _strip_similar_block(entries: list[dict]) -> list[dict]:
    """Strip match_type + similarity from similar_existing / similar_existing_lessons entries.

    These raw search-pipeline signals are opaque to agent reasoning for dedup
    decisions; action_hint encodes the actionable summary and is KEPT.

    Applied ONLY at the assembly points for similar_existing and
    similar_existing_lessons blocks (engram_add_observation, engram_add_lesson),
    NOT globally — preserving these fields in engram_query debug-mode results.
    """
    return [
        {k: v for k, v in entry.items() if k not in _SIMILAR_STRIP_KEYS}
        for entry in entries
    ]


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


def _rebuild_incidents_cache() -> dict:
    """Rebuild the error_incidents.json cache from the graph.

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


def _rebuild_cornerstone_anchors_cache() -> dict:
    """Rebuild the cornerstone_anchors.json cache from the graph.

    Sibling of _rebuild_incidents_cache for cornerstone targets (the
    "future cornerstone-tripwire mechanism" reserved in
    engram_register_exemplar — #1691). Walks `exemplifies` edges
    (exemplar → cornerstone) and pairs each exemplar with its
    cornerstone's claim + anchor line. The anchor line is the behavioral
    one-liner from metadata `anchor_line` (same voice as the
    warm-briefing anchor section); falls back to the claim when unset.

    Unlike lessons (whose exemplars are error incidents), cornerstone
    exemplars are ordinary claim-bearing nodes — the concrete situations
    the principle proved itself in. The surface hook matches situations
    to situations and reaches the principle via the edge, so more
    exemplars = more matching surface area.

    Idempotent; returns a summary dict with counts.
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT e.source_id AS exemplar_id, e.target_id AS cornerstone_id,
                      n.claim AS cs_claim, n.metadata AS cs_meta
               FROM edges e
               JOIN nodes n ON n.id = e.target_id
               WHERE e.relation = 'exemplifies' AND n.type = 'cornerstone'
                 AND n.is_current = 1"""
        ).fetchall()

        index: dict[str, dict] = {}
        cornerstone_count: set[str] = set()
        for r in rows:
            try:
                meta = json.loads(r["cs_meta"]) if r["cs_meta"] else {}
            except (TypeError, json.JSONDecodeError):
                meta = {}
            index[r["exemplar_id"]] = {
                "cornerstone_id": r["cornerstone_id"],
                "cornerstone_claim": r["cs_claim"],
                "anchor_line": meta.get("anchor_line", "") or r["cs_claim"],
            }
            cornerstone_count.add(r["cornerstone_id"])

        os.makedirs(os.path.dirname(CORNERSTONE_ANCHORS_PATH), exist_ok=True)
        with open(CORNERSTONE_ANCHORS_PATH, "w") as f:
            json.dump(index, f, indent=2)

        return {
            "status": "rebuilt",
            "exemplar_count": len(index),
            "cornerstone_count": len(cornerstone_count),
            "path": CORNERSTONE_ANCHORS_PATH,
        }
    finally:
        conn.close()


# Principle-trigger surfaces (#1698 slice 1): one row per
# (edge relation, principle type) pair. Each spec is
# (relation, principle_type, kind, nudge_metadata_key, mode, bidirectional).
# `tensions` is the relational-tier violation flavor (Lei 2026-07-07, design
# doc §7): a tension edge touching a goal makes the OTHER end a trigger, in
# either direction (tensions has no causal direction — see its
# EDGE_CLASSIFICATIONS entry).
_PRINCIPLE_TRIGGER_SPECS = (
    ("exemplifies", "lesson", "lesson", "scaffolding_nudge", None, False),
    ("exemplifies", "cornerstone", "cornerstone", "anchor_line", None, False),
    ("instantiates", "axiom", "axiom", "surfacing_nudge", None, False),
    ("serves", "goal", "goal", "surfacing_nudge", None, False),
    ("tensions", "goal", "goal", "surfacing_nudge", "tension", True),
)


def _rebuild_principle_triggers() -> dict:
    """Rebuild the unified principle_triggers.json registry (#1698 slice 1).

    One registry over all four principle kinds, derived entirely from edges
    per _PRINCIPLE_TRIGGER_SPECS. Entry shape (list-valued since #1731):
        trigger_node_id -> [{principle_id, kind, claim, nudge,
                             situation_pattern?, mode?}, ...]

    List-valued keyspace (#1731 fix): a dual-role trigger node — one that
    both exemplifies a lesson AND instantiates an axiom, say — used to
    collide under the original single-dict-per-trigger keyspace
    (last-spec-wins, silently dropping whichever spec ran earlier in
    _PRINCIPLE_TRIGGER_SPECS; lesson ran first, so it always lost to axiom).
    That was a real, live incident: a dual-role node's lesson tripwire went
    silently dark on first deploy (issue #1731) because the unified read
    path (slice 2) has no legacy fallback for a trigger the registry can see
    but only under the WRONG kind. Every (trigger, principle) relationship
    now gets its own list entry; the consumer iterates all of them per
    matched trigger and lets the existing cap/priority logic (lesson >
    axiom > cornerstone > goal) arbitrate, exactly as it already does across
    DIFFERENT triggers matched in the same prompt.

    Migration shim: this is ADDITIVE — the legacy caches
    (error_incidents.json, cornerstone_anchors.json) keep being written by
    their own rebuilds and remain what the hooks read until the unified
    check lands (design doc §2, implementation PR 2 of 3).

    Idempotent full rewrite; returns a summary dict; never raises past the
    connection (callers wrap best-effort).
    """
    conn = _get_db()
    try:
        index: dict[str, list[dict]] = {}
        by_kind: dict[str, int] = {}
        for relation, ptype, kind, nudge_key, mode, bidirectional in _PRINCIPLE_TRIGGER_SPECS:
            # Trigger-side is_current fix (#1698, Luria's catch, confirmed by
            # Sol reviewing #1702): the join previously filtered only the
            # principle side (n.is_current = 1); a retracted/superseded
            # trigger node still landed in the registry keyed by its node
            # ID. Added join on the trigger side (t), filtered
            # is_current = 1, in both the forward and bidirectional-reverse
            # queries below.
            rows = conn.execute(
                """SELECT e.source_id AS trigger_id, e.target_id AS principle_id,
                          n.claim AS claim, n.metadata AS meta
                   FROM edges e
                   JOIN nodes n ON n.id = e.target_id
                   JOIN nodes t ON t.id = e.source_id
                   WHERE e.relation = ? AND n.type = ? AND n.is_current = 1
                     AND t.is_current = 1""",
                (relation, ptype),
            ).fetchall()
            if bidirectional:
                rows = list(rows) + list(conn.execute(
                    """SELECT e.target_id AS trigger_id, e.source_id AS principle_id,
                              n.claim AS claim, n.metadata AS meta
                       FROM edges e
                       JOIN nodes n ON n.id = e.source_id
                       JOIN nodes t ON t.id = e.target_id
                       WHERE e.relation = ? AND n.type = ? AND n.is_current = 1
                         AND t.is_current = 1""",
                    (relation, ptype),
                ).fetchall())
            for r in rows:
                if r["trigger_id"] == r["principle_id"]:
                    continue
                try:
                    meta = json.loads(r["meta"]) if r["meta"] else {}
                except (TypeError, json.JSONDecodeError):
                    meta = {}
                entry: dict = {
                    "principle_id": r["principle_id"],
                    "kind": kind,
                    "claim": r["claim"],
                    "nudge": meta.get(nudge_key, "") or r["claim"],
                }
                pattern = meta.get("situation_pattern", "")
                if pattern:
                    entry["situation_pattern"] = pattern
                if mode:
                    entry["mode"] = mode
                index.setdefault(r["trigger_id"], []).append(entry)

        # Count every surviving entry across every trigger's list — #1731
        # removed the last-spec-wins collision, so a dual-role trigger (one
        # node with two-plus entries) now correctly counts once per kind,
        # same as #1702's original "count survivors, not rows processed"
        # intent, just generalized to a list instead of a single winner.
        for entries in index.values():
            for entry in entries:
                k = entry["kind"]
                by_kind[k] = by_kind.get(k, 0) + 1

        os.makedirs(os.path.dirname(PRINCIPLE_TRIGGERS_PATH), exist_ok=True)
        with open(PRINCIPLE_TRIGGERS_PATH, "w") as f:
            json.dump(index, f, indent=2)

        return {
            "status": "rebuilt",
            "trigger_count": len(index),
            "by_kind": by_kind,
            "path": PRINCIPLE_TRIGGERS_PATH,
        }
    finally:
        conn.close()


def _with_state_lock(lock_path, fn):
    """Run fn() (a read-modify-write closure) while holding an exclusive,
    BLOCKING flock on lock_path. Unlike #1709's non-blocking pattern, this
    one blocks -- the critical section is a fast local file read+write (no
    daemon round-trip), so a brief wait is cheap and correctness (no lost
    update) matters more than never-block here. Degrades to running fn()
    unlocked if fcntl/lockfile-open fails (non-POSIX, permissions) -- same
    never-crash contract as #1709's degrade path, just without the
    non-blocking contention branch (there's nothing to suppress; fn() still
    runs, just unprotected).

    #1720: this exact ~15-line helper is duplicated in
    src/engram/hooks/claude/engram-surface-hook.py rather than shared -- this
    module (engram_core.py, MCP server process) and the hook script (a
    separate subprocess invoked by Claude Code) have no shared import surface
    today, and a new shared module for ~15 lines isn't worth the engineering
    (see spec docs/specs/1720-principle-state-write-race.md). If either copy
    changes, check the other.
    """
    try:
        import fcntl
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except Exception as exc:
        # #1743: this degrade was silent -- nothing distinguished "locked and
        # protected" from "running unlocked," the same diagnosis-costing gap
        # #1737/#1742 fixed for the non-blocking cooldown lock. Emit one
        # never-block/never-raise stderr tell (hook/server stderr goes to logs,
        # not the model, so it is safe on every call) so a fallback-to-unlocked
        # -- which leaves fn()'s read-modify-write unprotected against a lost
        # update -- leaves a trace instead of being invisible. (#1742's
        # state-file-flag half does not map onto this generic helper, which has
        # no access to fn()'s state dict; the stderr line is the proportionate
        # tell here.) Kept byte-identical with the sibling copy (surface-hook /
        # engram_core) per the #1720 sync note above -- if one changes, change
        # the other.
        try:
            import sys
            print(
                f"[engram _with_state_lock] flock degraded to UNLOCKED "
                f"({lock_path}): {type(exc).__name__}: {exc} -- read-modify-write "
                f"is unprotected; a concurrent write may be lost",
                file=sys.stderr,
            )
        except Exception:
            pass
        return fn()  # degrade: run unlocked rather than crash or skip
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking -- no LOCK_NB
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass


def _reset_principle_enactments(principle_id: str) -> None:
    """Reset enactments to 0 for principle_id in principle-trigger-state.json
    (#1698 slice 3 §3 — "registering a NEW exemplar/incident against the
    principle resets enactments to 0 (full strength)").

    Best-effort: a write failure here must never fail the exemplar-
    registration call it's attached to.

    Cross-process write (spec §3 flag for reviewer): this file has, through
    slices 1-2, only ever been touched by the hook scripts (separate
    subprocesses invoked by Claude Code); this is the first time server-side
    (MCP process) code writes it. Path is resolved via DATA_DIR (the same
    module-level global the hooks' own ENGRAM_HOME resolution is meant to
    mirror — see `_configure_paths` above for the single source of truth on
    path redirection, e.g. under test isolation) rather than hand-rolling a
    second resolution scheme. Write is atomic (tmp + os.replace), matching
    the hooks' own pattern, so a concurrent hook-side read never sees a
    partial write.

    #1720: the read-modify-write below now runs under a blocking flock (see
    `_with_state_lock`) to close the LOST-UPDATE race flagged in colleague
    review (Ariadne, PR #1717): the atomicity claim above only ever covered
    TORN READS, not a concurrent hook-side read-modify-write silently
    dropping whichever side loses (last-writer-wins on the whole file). Same
    TOCTOU class #1709 fixed with flock, but blocking rather than
    non-blocking-suppress -- see docs/specs/1720-principle-state-write-race.md
    "Why NOT #1709's non-blocking-suppress pattern" for why this path can't
    use #1709's shape (this call site is not on the render-decision path, so
    blocking briefly here is safe).
    """
    path = os.path.join(DATA_DIR, "principle-trigger-state.json")
    lock_path = path + ".lock"

    def _do_reset():
        try:
            with open(path, "r") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        entry = state.get(principle_id)
        if isinstance(entry, int):
            entry = {"last_fired_prompt": entry, "strength": 1.0, "enactments": 0, "fires": 0}
        if not isinstance(entry, dict):
            entry = {"last_fired_prompt": 0, "strength": 1.0, "enactments": 0, "fires": 0}
        entry["enactments"] = 0
        state[principle_id] = entry
        try:
            tmp = str(path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except OSError:
            pass

    _with_state_lock(lock_path, _do_reset)


# ---------------------------------------------------------------------------
# #872 wave-5 rolling promotions — _create_derivation (shared by B + E)
# ---------------------------------------------------------------------------
# Canonical copies live here; server.py originals deleted in the same commit
# (PROMOTION-FIDELITY RULE). Verbatim move from server.py — no simplification.
# E family (_resolve_impl) moves in wave 6 and will call core._create_derivation
# directly from engram_resolve.py.

# Excerpt length for warning surfacing (claim + retraction_reason).
# Long enough to carry gist, short enough to keep responses compact.
_WARNING_EXCERPT_LEN = 140

# Structural expectations per reasoning type — used for validation warnings.
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


def _open_contradiction_ids(conn: sqlite3.Connection, node_id: str) -> list:
    """Return IDs of live, unresolved contradiction nodes disputing node_id.

    "Open" per #1654's settled semantics: no `resolves` closure has landed
    yet. A contradiction's status only leaves 'active' (→ 'resolved' or
    'partially_resolved') once a resolves edge is wired against it
    (_resolve_impl), so status='active' IS the "no resolves closure" test
    from the issue text — matches the existing obsolescence-scan convention
    (engram_query._pattern_contradiction_obsolescence_ready).
    """
    rows = conn.execute(
        """SELECT DISTINCT ct.id FROM edges e
           JOIN nodes ct ON ct.id = e.source_id
           WHERE e.target_id = ? AND e.relation = 'contradicts'
             AND ct.type = 'contradiction' AND ct.is_current = 1
             AND ct.status = 'active'
           ORDER BY ct.id""",
        (node_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def _validate_premises(
    supporting_ids: list,
    conn: sqlite3.Connection,
    use_stale: bool = False,
    use_contested: bool = False,
) -> tuple[Optional[dict], list, list]:
    """MECH-5 taint/stale/contradicted blocking guard for engram_derive.

    Classifies each supporting premise and blocks derivations built on
    compromised foundations — the Mao-Cao compounding pattern that
    the diagnosed-pattern derivation diagnosed.

    Rules:
      - Any premise with metadata.tainted_by → hard block (BLOCKED_TAINTED).
        No override. Taint means an upstream was retracted (proven wrong).
      - Any premise sitting on the open side of an unresolved contradiction
        (a live ct_ node, no resolves closure) AND use_contested=False →
        soft block (BLOCKED_CONTRADICTED, #1654). Agent opts in with
        use_contested=True — a contradiction is a live dispute, not a
        refutation, so building on it is a deliberate, loudly-marked
        choice, not a hard wall. Checked before stale (a disputed claim is
        a more acute concern than a merely-superseded one).
      - Any premise with metadata.stale_by AND use_stale=False →
        soft block (BLOCKED_STALE). Agent opts in with use_stale=True
        when the upstream update is judged irrelevant to current logic.
      - Mixed taint + (contradicted|stale) → treated as tainted (taint
        dominates — it is a hard block with no override, and short-
        circuits both other checks for that premise).
      - Contested and stale are NOT mutually exclusive with each other: a
        premise can be both superseded AND sitting on a still-open
        contradiction (e.g. supersede fires on one side of a live dispute
        without auto-resolving it — issue #229's documented workflow).
        Both are classified independently, so BOTH overrides
        (use_contested AND use_stale) must clear before such a premise is
        allowed through — clearing only one would silently drop the
        other's guard.

    Returns:
        (block_response, stale_ids, contested_ids)
        - block_response: None if premises are clean (or opt-ins cover
          them). Otherwise a dict ready for json.dumps with a structured
          block error.
        - stale_ids: list of premise IDs that are stale (for the caller
          to stamp metadata.built_on_stale when use_stale=True lets us
          proceed).
        - contested_ids: list of premise IDs sitting on an open
          contradiction (for the caller to auto-stamp
          metadata.built_on_contested when use_contested=True lets us
          proceed — the stamp is NEVER author-supplied, only ever written
          by this override path, so it can't be gamed/omitted).
    """
    tainted_premises = []
    contested_premises = []
    stale_premises = []
    stale_ids: list = []
    contested_ids: list = []
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

        # Taint dominates unconditionally, so only compute contested/stale
        # when the premise isn't already tainted. But contested and stale
        # are NOT mutually exclusive with each other — a premise can be
        # BOTH superseded AND sitting on a still-open contradiction (e.g.
        # supersede fires on one side of a live dispute; the contradiction
        # isn't auto-resolved by that — issue #229's documented workflow).
        # Classifying one branch (via elif) silently drops the other's
        # signal, letting a premise slip past BLOCKED_STALE (or vice versa)
        # just because it also happened to be contested. Track both
        # independently so each applicable override is required on its own.
        open_ct_ids = [] if tainted_by else _open_contradiction_ids(conn, sid)

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
        else:
            if open_ct_ids:
                contested_premises.append({
                    "id": sid,
                    "open_contradictions": open_ct_ids,
                })
                contested_ids.append(sid)
            if stale_by:
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

    # Taint dominates — block even if use_stale/use_contested=True.
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
        }, stale_ids, contested_ids)

    # #1654 — a premise under an open, unresolved contradiction is a live
    # dispute, not (yet) a refutation. Block by default so new derivations
    # don't silently compound on contested ground; the override is a
    # deliberate, loudly-marked choice (mirrors use_stale/BLOCKED_STALE).
    if contested_premises and not use_contested:
        return ({
            "status": "blocked_contradicted",
            "error": "BLOCKED_CONTRADICTED: premise(s) sit on an open, unresolved contradiction.",
            "contested_premises": contested_premises,
            "guidance": (
                "Either resolve the contradiction first (engram_resolve against "
                "the ct_ node), or retry with use_contested=True if you're "
                "deliberately building on the contested side while the dispute "
                "is still open — writes a metadata.built_on_contested audit "
                "marker (auto-stamped, not author-supplied) so resolution "
                "pressure on the open contradiction stays measurable."
            ),
        }, stale_ids, contested_ids)

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
        }, stale_ids, contested_ids)

    return (None, stale_ids, contested_ids)


def _trace_evidence_roots(conn: sqlite3.Connection, node_id: str, max_depth: int = 4096) -> set:
    """Trace a node back to its evidence root(s) through citation edges.

    ``max_depth`` is a per-VISIT budget (decremented once per dequeued node),
    NOT a depth limit — the name is historical. Termination is already
    guaranteed by the ``visited`` set (each node is processed once over a
    finite reachable subgraph); the budget is only an anti-pathological cap.
    It was formerly 10, which silently TRUNCATED any premise whose evidence sat
    more than ~10 node-visits down a layered derivation spine, returning a
    partial or empty root set — the root cause of the #1186 multi_source
    false-positive. 4096 cannot truncate a real provenance graph.
    """
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
            "SELECT target_id FROM edges WHERE source_id = ? AND relation IN ('cites', 'derives_from', 'supported_by')",
            (current,),
        ).fetchall()
        for e in edges:
            queue.append(e["target_id"])

    return roots


def _trace_observation_leaves(
    conn: sqlite3.Connection, node_id: str, max_depth: int = 4096
) -> set:
    """Trace a node back to the OBSERVATION leaves it rests on.

    Walks the derivation spine (``derives_from`` / ``supported_by``) backward
    and collects nodes of ``type LIKE 'observation_%'`` — the layer where the
    standpoint axes (author/collection/lineage) and ``quote_type``/``fs_class``
    actually live. Terminates AT each observation: it does NOT descend past an
    observation into its own ``cites``→evidence hops, because evidence nodes
    carry neither standpoint nor quote_type (descending would lose the data).
    If the start node is itself an observation, returns ``{node_id}``.

    Why not ``_trace_evidence_roots``: that returns ``type == 'evidence'`` roots,
    which carry no standpoint/quote_type — aggregating standpoint or F-S over
    those yields all-None/all-unknown and the block silently never fires on a
    layered derivation (whose direct premises are themselves derivations).
    The standpoint/F-S leaves are observations, one layer up from evidence.
    (Ariadne #49 — the prior ``_trace_evidence_roots`` reuse was the wrong layer.)

    Mirrors ``_trace_evidence_roots``'s per-visit-budget semantics (``max_depth``
    decremented per dequeued node, NOT a depth limit; termination guaranteed by
    ``visited``) for behavioral parity — including the raised budget (#1186): at
    the former 10 a deep derivation spine truncated the leaf set, which on this
    (all-or-omit-gated) consumer caused silent under-credit rather than a
    false-positive, but is the same latent defect.
    """
    visited = set()
    queue = [node_id]
    leaves = set()

    while queue and max_depth > 0:
        max_depth -= 1
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        node = conn.execute("SELECT type FROM nodes WHERE id = ?", (current,)).fetchone()
        if not node:
            continue
        if node["type"].startswith("observation"):
            leaves.add(current)
            continue  # terminate AT the observation — do not follow its cites→evidence

        # Non-observation (derivation / goal / etc.): follow the premise spine
        # only — NOT 'cites' (an observation's cites→evidence is the layer we
        # stop above; a derivation's cites→context is not a premise).
        edges = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation IN ('derives_from', 'supported_by')",
            (current,),
        ).fetchall()
        for e in edges:
            queue.append(e["target_id"])

    return leaves


_LINEAGE_RE = re.compile(r"[a-z0-9_-]+:[a-z0-9._-]+")

# Once-per-process sentinel: ensures the malformed-self_lineage warning fires
# at most once, even if _self_lineage() is called on every observation (#960).
_self_lineage_warned: bool = False


def _self_lineage() -> str:
    """The install's own training lineage (provider:family) from config.json's
    'self_lineage' key; '' if unset or malformed (fail-closed).

    Used by null=self (D1 §2, Borges #721): an unmarked observation is the
    filer's own lineage by convention. Read-time only — never written to the
    standpoint_lineage column, so the actionability gate (_graph_lineage_count)
    still counts only explicitly-marked lineages. Returns '' on any error or
    if the value does not match provider:family format — safe degradation: null=self
    does not engage and the gate stays closed, same as pre-feature behavior
    (Luria's #723 deployment note). Fail-closed is critical: a malformed value
    would hash into a different cluster than correctly-marked same-lineage
    premises, producing false diversity and suppressing ⚠⚠ (Mira #750)."""
    global _self_lineage_warned
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        val = (cfg.get("self_lineage") or "").strip()
        if val and not _LINEAGE_RE.fullmatch(val):
            if not _self_lineage_warned:
                _self_lineage_warned = True
                logging.getLogger(__name__).warning(
                    "self_lineage config value %r is malformed — expected "
                    "provider:family (e.g. 'anthropic:opus'); "
                    "ignoring and treating as unset (fail-closed, #960).",
                    val,
                )
            return ""
        return val
    except Exception:
        return ""


def _standpoint_cluster_key(
    conn: sqlite3.Connection, node_id: str
) -> dict | None:
    """Return per-axis cluster keys for a node's standpoint fields.

    Returns {"author": sha256(author)[:12] | None, "collection": sha256(collection)[:12] | None,
    "lineage": sha256(lineage)[:12] | None, "architecture": sha256(architecture)[:12] | None}.
    Returns None if no standpoint field is set (can't distinguish from 'no data').

    Note: standpoint_override_tag is annotation-only — it is NOT included in
    cluster-key computation. It feeds the diagnostic prompt; a node with only
    an override_tag set returns None here (treated as no standpoint data).
    standpoint_override_tag will serve as the entry point for platform/env/locale
    data until v3 makes those axes first-class.

    standpoint_architecture is an optional enum field tracking cognitive
    architecture diversity (transformer, human, etc.). No null=self fallback —
    an unset architecture is truly unknown, not synthesized.
    """
    row = conn.execute(
        "SELECT standpoint_author_id, standpoint_collection_id, standpoint_lineage, "
        "standpoint_architecture "
        "FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if not row:
        return None
    author = (row["standpoint_author_id"] or "").strip()
    collection = (row["standpoint_collection_id"] or "").strip()
    lineage = (row["standpoint_lineage"] or "").strip()
    architecture = (row["standpoint_architecture"] or "").strip()
    # null=self at the lineage axis (D1 §2 — Borges #721): an unmarked
    # observation is the filer's OWN training lineage by convention. Fall back
    # to config self_lineage so own-unmarked premises make the gate fire.
    # Lineage axis ONLY — do NOT synthesize author/collection/architecture (the
    # agent asserted none of those). DB column stays NULL (read-time synthesis),
    # so _graph_lineage_count still counts only explicitly-marked lineages
    # (Luria's #723 sign-off: actionability gate unaffected).
    if not lineage:
        lineage = _self_lineage()
    if not author and not collection and not lineage and not architecture:
        return None
    return {
        "author": hashlib.sha256(author.encode()).hexdigest()[:12] if author else None,
        "collection": hashlib.sha256(collection.encode()).hexdigest()[:12] if collection else None,
        "lineage": hashlib.sha256(lineage.encode()).hexdigest()[:12] if lineage else None,
        "architecture": hashlib.sha256(architecture.encode()).hexdigest()[:12] if architecture else None,
    }


def _graph_lineage_count(conn: sqlite3.Connection) -> int:
    """Distinct non-null standpoint_lineage values across the graph.

    The v3 actionability gate for the standalone lineage ⚠: the warning's
    remedy ("add cross-lineage support") is only available when a
    cross-lineage premise is actually citable, which the gate reads from
    GRAPH STATE, not install topology/config — a solo install that records
    cross-lineage-sourced observations has earned the ⚠; a multi-agent
    monoculture house has not. Self-adjusting: the first recorded
    cross-lineage claim flips the axis live.
    """
    # Two filters ENFORCE the docstring's "citable claim-bearing premise" domain
    # (rather than assert it), so the gate counts only rows that can actually be
    # cited as a cross-lineage premise:
    #   (1) is_current = 1 (Mira #48): a retracted/superseded node is not citable.
    #   (2) type LIKE 'observation_%' (Mira #954): standpoint_lineage is a
    #       SOURCE-provenance field, set only via the observation write path
    #       (engram_observation.py) — derivations/goals/etc. take no standpoint and
    #       are not citable cross-lineage premises, so a non-observation row
    #       carrying a stray lineage value must not activate the gate. We match
    #       the whole observation FAMILY by prefix rather than a hand-list of
    #       specific types: today the family is {observation_factual,
    #       observation_predictive} (both written by the same observation path and
    #       both citable premises), and a hand-list 'observation_factual' silently
    #       UNDER-counts predictive rows and re-opens this gap on every new
    #       observation_* type. The prefix predicate covers new family members by
    #       default — the growing-family guard closing form (glob the family, never
    #       a hand-maintained positive list), applied here because this exact gap
    #       has recurred three times (is_current, then type, then type-incomplete).
    row = conn.execute(
        "SELECT COUNT(DISTINCT standpoint_lineage) AS c FROM nodes "
        "WHERE is_current = 1 "
        "  AND type LIKE 'observation_%' "
        "  AND standpoint_lineage IS NOT NULL AND TRIM(standpoint_lineage) != ''"
    ).fetchone()
    return row["c"] if row else 0


# Phase-1 F-S proxy prior (D1: quote_type is the DEFAULT/prior, demoted the
# moment the real F-S field exists). Middle quote_types (official_statement,
# attributed_analysis, unnamed_source) deliberately map to "unknown" — they
# are quote-checkable, not re-executable, and the prior should not overclaim.
_FS_PROXY_MAP = {
    "hard_data": "re-executable",
    "editorial": "frozen",
    "personal_communication": "frozen",
}


def _node_fs_class(conn: sqlite3.Connection, node_id: str) -> tuple[str, str]:
    """Return (fs_class, source) for a node's falsification-sensitivity.

    fs_class: "re-executable" | "frozen" | "unknown"
    source:   "proxy:quote_type" (Phase-1 default) | "field" (Phase-2 real field)

    Phase-1/Phase-2 contract seam (standpoint v3 design §4.2): Phase 1 derives
    a proxy from quote_type at READ TIME — never persisted, so Phase 2's real
    F-S field cannot collide with stale proxy data and needs no migration.
    Phase 2 swaps these internals to read the field; no caller changes.
    Surfaced text must always carry the proxy label while source is a proxy
    (never present a default as a measurement).
    """
    row = conn.execute(
        "SELECT fs_class, quote_type FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if not row:
        return ("unknown", "proxy:quote_type")
    # Phase-2 path: native field present
    if row["fs_class"] in ("re-executable", "frozen"):
        return (row["fs_class"], "field")
    # Phase-1 fallback: derive from quote_type proxy
    if not row["quote_type"]:
        return ("unknown", "proxy:quote_type")
    return (_FS_PROXY_MAP.get(row["quote_type"], "unknown"), "proxy:quote_type")


def _prediction_resolved(conn: sqlite3.Connection, leaf_id: str) -> bool:
    """Whether a predictive observation's event has been adjudicated by reality.

    Phase-1: conservatively False — a prediction does not anchor until reality
    closes it, and the substrate has no resolution field yet (predicted_event /
    resolution_timeframe are descriptive only). Phase-2 can swap in a real
    resolution check with no caller change. (Component D — Borges #721.)"""
    return False


def _leaf_verified(conn: sqlite3.Connection, leaf_id: str) -> bool:
    """State-3 (reality-VERIFIED) proxy for an observation leaf (D1 §5,
    Component D — Borges #721/#730).

    verifi-ABILITY (state-2) is NOT enough — Lei's verifiable≠verified axis.
    ONLY the claim's content-type earns state-3: hard_data (measured / observed
    event) or official_statement (publicly-checkable record). A file:// quote-
    check is quote-PROVENANCE, not claim-reality-grounding — Lei's ruling: "you
    can verify my quote in the chat log, but it doesn't make the statement more
    true" — so there is deliberately NO file:// branch; an accurately-quoted
    opinion (personal_communication / editorial) stays state-2. Fully
    determinate (no unknown bucket), so the ⚠⚠ composite drops its n_unknown
    honesty floor (Luria #723)."""
    row = conn.execute(
        "SELECT type, quote_type FROM nodes WHERE id = ?", (leaf_id,)
    ).fetchone()
    if not row:
        return False
    # Predictive: unverified until the predicted event resolves — regardless of
    # quote_type (an official statement about a FUTURE event is still a
    # prediction, not reality-closure).
    if row["type"] == "observation_predictive" and not _prediction_resolved(conn, leaf_id):
        return False
    return row["quote_type"] in ("hard_data", "official_statement")


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

        # Judge same-source over the ROOTED SUBSET (Aleph, #1188 colleague
        # review): warn only when >= 2 premises actually RESOLVED evidence roots
        # AND those roots collapse to a single shared one. Rootless premises (an
        # axiom cited as support, or — pre-#1186 — a budget-truncated trace)
        # contribute nothing to all_roots and are EXCLUDED from the judgment,
        # never allowed to veto it. This closes both failure modes:
        #   * old `len(all_roots) <= 1` fired on the len==0 case → false-flagged
        #     genuinely-disjoint premises as same-source (#1186);
        #   * a blunt all-or-omit `all(rooted)` would let a single rootless
        #     premise SUPPRESS a real same-source warning among the others
        #     (the false-negative — e.g. [ob_A->ev1, ob_B->ev1, ax_C]).
        # (rooted premises' roots == all_roots, since rootless add the empty set.)
        rooted_count = sum(1 for r in evidence_roots.values() if r)
        if rooted_count >= 2 and len(all_roots) == 1:
            warnings.append(
                f"Reasoning type '{reasoning_type}' requires independent sources, "
                f"but all premises trace back to the same evidence node. "
                f"Multiple observations from the same source are not independent — "
                f"consider 'deductive_hypothetical_syllogism' or 'inductive_causal' instead."
            )

        # Register disclosure (issue #933): name the mechanical check that RAN
        # — whether it passed or failed (its pass/fail is conveyed by the
        # presence/absence of the same-evidence warning above). Without this
        # line a clean pass reads as full premise-quality coverage. Per the
        # class-5 two-layer lesson: a detector's silence must be
        # distinguishable from a detector that never looked.
        warnings.append(
            "CHECKED (mechanical): same-evidence-root cluster across premises."
        )

    # Standpoint uniformity check — advisory for ALL derivations with 1+ premises.
    # Extended from n >= 2 to n >= 1 (#958): chains of single-premise derivations
    # (dv←dv←dv←ob) previously escaped the F-S and ⚠⚠ checks at every level.
    # STANDPOINT axis reports for n_leaves=1 are trivially uniform but technically
    # correct; F-S and ⚠⚠ are the primary signals for the single-premise case.
    #
    # Implementation note: standpoint_author_id / standpoint_collection_id are
    # filed on observation (premise) nodes, not on evidence nodes. We trace
    # evidence roots to determine whether premises share the same source for the
    # multi_source check above, but the standpoint cluster key is derived from
    # the premise nodes themselves (where the standpoint data actually lives).
    if n >= 1:
        # Closure-walk to OBSERVATION leaves: standpoint (author/collection/
        # lineage) + quote_type/fs_class live on observation nodes — NOT on the
        # direct premises (which may themselves be derivations) and NOT on the
        # evidence roots. For a layered derivation the direct premises are
        # derivations → no standpoint, fs=unknown → the block would silently
        # never fire. Aggregate standpoint + F-S over the observation_% leaves at
        # the bottom of the derives_from/supported_by spine. (Ariadne #49 — the
        # prior _trace_evidence_roots reuse was the wrong layer: it returns ev_
        # roots, which carry neither standpoint nor quote_type.)
        obs_leaves = sorted(
            {leaf for pid in premise_ids
             for leaf in _trace_observation_leaves(conn, pid)}
        )
        n_leaves = len(obs_leaves)

        # Set by the lineage-axis block when all leaves carry lineage data and
        # share one cluster; consumed by the ⚠⚠ composite (now in the sibling
        # FALSIFICATION block). Initialized here so the F-S block can read it
        # even when the standpoint block is skipped.
        lineage_uniform = False

        # --- STANDPOINT block: gated on all leaves carrying standpoint data ---
        if obs_leaves:
            axis_keys = [_standpoint_cluster_key(conn, lid) for lid in obs_leaves]
            if all(k is not None for k in axis_keys):
                # All premises have standpoint data — report per axis so
                # author-diverse/collection-uniform can't hide behind the combined key.
                def _axis_label(vals):
                    # All-or-omit: if ANY premise has None for this axis, the
                    # axis is partially tracked — omitting prevents asserting
                    # uniformity from a single premise's data.
                    if None in vals:
                        return None
                    n_clusters = len(vals)
                    unit = "cluster" if n_clusters == 1 else "clusters"
                    verdict = "diverse" if n_clusters > 1 else "⚠ uniform"
                    return f"{n_clusters} {unit} / {n_leaves} leaves ({verdict})"

                parts = []
                author_lbl = _axis_label({k["author"] for k in axis_keys})
                collection_lbl = _axis_label({k["collection"] for k in axis_keys})
                if author_lbl:
                    parts.append(f"author: {author_lbl}")
                if collection_lbl:
                    parts.append(f"collection: {collection_lbl}")

                # Lineage axis (v3): same all-or-omit guard as the other axes,
                # but the UNIFORM verdict carries an actionability gate — the
                # standalone ⚠ renders only when the graph itself demonstrates
                # >= 2 distinct lineage values (a cross-lineage premise is
                # actually citable). Otherwise informational, teaching the
                # enable-path (a warning must point to an available action).
                lineage_vals = {k["lineage"] for k in axis_keys}
                if None not in lineage_vals:
                    if len(lineage_vals) > 1:
                        parts.append(f"lineage: {len(lineage_vals)} clusters / {n_leaves} leaves (diverse)")
                    else:
                        lineage_uniform = True
                        if _graph_lineage_count(conn) >= 2:
                            parts.append(
                                f"lineage: 1 cluster / {n_leaves} leaves (⚠ uniform — shared training "
                                f"lineage; zero independent corroboration on "
                                f"substrate-prior bias)"
                            )
                        # else: single-lineage graph — emit NO standalone line.
                        # null=self (Component A) makes lineage uniform on EVERY
                        # own derivation, so the old informational line would fire
                        # constantly = the per-derivation fatigue Lei's precision
                        # gate forbids (Borges #721). The ⚠⚠ composite carries the
                        # only actionable signal here (when zero-verified).

                    # Layer-2 check (#1289): inductive_generalization requires
                    # >= 2 INDEPENDENT cross-lineage instances — the hypothesis-
                    # author's own lineage is the variance source and cannot
                    # simultaneously serve as independent corroboration.
                    # lineage_vals holds sha256[:12] hashes (from
                    # _standpoint_cluster_key), so self_lin must be hashed
                    # to the same form before the set-difference.
                    # Only fires when the graph has >= 2 distinct lineages
                    # (warning must point to an available action).
                    if reasoning_type == "inductive_generalization":
                        self_lin = _self_lineage()
                        self_lin_hash = (
                            hashlib.sha256(self_lin.encode()).hexdigest()[:12]
                            if self_lin else None
                        )
                        independent = (
                            lineage_vals - {self_lin_hash}
                            if self_lin_hash else lineage_vals
                        )
                        # Count observation leaves with a cross-lineage (non-author) lineage hash.
                        cross_lin_set = {self_lin_hash} if self_lin_hash else set()
                        n_cross_leaves = sum(
                            1 for k in axis_keys
                            if k.get("lineage") is not None
                            and k.get("lineage") not in cross_lin_set
                        )
                        if len(independent) < 2 and _graph_lineage_count(conn) >= 2:
                            parts.append(
                                f"⚠ inductive_generalization: independent cross-lineage instances "
                                f"(excluding hypothesis-author lineage '{self_lin}') = {len(independent)}; "
                                f"{n_cross_leaves} of {n_leaves} leaves cross-lineage — consider inductive_analogy"
                            )

                # Architecture axis: collect raw enum values directly from DB
                # so we can render the actual architecture name in warnings.
                # Separate from axis_keys (which holds hashes) — raw values
                # are needed to name the specific family in the Class A warning.
                # No null=self — an unset architecture is genuinely unknown.
                # All-or-omit: collect every leaf's value (None if unset), then
                # emit only when every leaf has data (mirrors _axis_label guard).
                arch_raw_list = []
                for lid in obs_leaves:
                    arch_row = conn.execute(
                        "SELECT standpoint_architecture FROM nodes WHERE id = ?",
                        (lid,),
                    ).fetchone()
                    val = (
                        arch_row["standpoint_architecture"].strip().lower()
                        if (arch_row and arch_row["standpoint_architecture"])
                        else None
                    )
                    arch_raw_list.append(val)
                if arch_raw_list and None not in arch_raw_list:
                    arch_set = set(arch_raw_list)
                    if len(arch_set) > 1:
                        parts.append(
                            f"architecture: {len(arch_set)} families (diverse)"
                        )
                    else:
                        arch_name = next(iter(arch_set))
                        parts.append(
                            f"architecture: 1 family"
                            f" (⚠ single-family (all {arch_name})"
                            f" — Class A exposure elevated)"
                        )
                # Partial coverage or no data: emit nothing (all-or-omit).

                # parts may be empty if all axes have partial coverage across
                # premises — the guard is load-bearing in the mixed-axis case.
                # Detect genuine mixed-axis partial coverage: at least one leaf
                # has data AND at least one leaf lacks data for the same axis.
                # Lineage excluded — null=self means it is never partially tracked;
                # suppression by the actionability gate (uniform, single-lineage
                # graph) is intentional and should produce silence, not a notice.
                has_partial_author = (
                    any(k["author"] is not None for k in axis_keys)
                    and any(k["author"] is None for k in axis_keys)
                )
                has_partial_collection = (
                    any(k["collection"] is not None for k in axis_keys)
                    and any(k["collection"] is None for k in axis_keys)
                )
                has_partial_arch = (
                    any(r is not None for r in arch_raw_list)
                    and any(r is None for r in arch_raw_list)
                )
                if parts:
                    warnings.append(
                        f"STANDPOINT: {'; '.join(parts)}; others unchecked."
                        " (load skill `engram-standpoint` for the calibration theory)"
                    )
                elif has_partial_author or has_partial_collection or has_partial_arch:
                    # All leaves have standpoint keys (none are None) but every
                    # axis has at least one leaf missing that specific axis value
                    # — mixed-axis partial coverage, nothing emitted above.
                    warnings.append(
                        "STANDPOINT: partially unchecked"
                        " (mixed-axis coverage — no axis has data on all premises)."
                    )
                # else: all axes absent or actionability-gated (e.g. uniform
                # single-lineage graph) — emit nothing by design.

            else:
                # Some leaves lack standpoint data — can't distinguish no-data
                # from same-cluster, so emit a low-noise unchecked notice (#761).
                n_missing = sum(1 for k in axis_keys if k is None)
                warnings.append(
                    f"STANDPOINT: unchecked ({n_missing}/{n_leaves} premises"
                    f" lack standpoint data)."
                )
            # The FALSIFICATION block below is NOT gated on standpoint — it runs
            # as a sibling so F-S (which needs no standpoint) is never suppressed
            # by missing standpoint marks.

        # --- FALSIFICATION block (UN-NESTED — sibling of STANDPOINT) ---
        # F-S is a property of the EVIDENCE, derived from quote_type/fs_class via
        # _node_fs_class — present on every observation, needs ZERO standpoint
        # data. Previously this block was nested inside the standpoint
        # all-non-None gate, so an unmarked premise silently suppressed the F-S
        # line too. Un-nested here, guarded only by its own known_fs check, over
        # the observation leaves. (Ariadne #49 — the un-nest.)
        if obs_leaves:
            fs_classes = [_node_fs_class(conn, lid) for lid in obs_leaves]
            known_fs = [(c, s) for c, s in fs_classes if c != "unknown"]
            if known_fs:
                # Scan known_fs only — unknown leaves return proxy source and
                # must not poison native-fielded sets.
                any_proxy = any(s.startswith("proxy:") for c, s in known_fs)
                # Per-source honesty (Ariadne #41/614, choice (a)): split by
                # (class, source) pair — native leaves render without the proxy
                # label and "-leaning" qualifier; proxy leaves carry both.
                # Denominator = observation-leaf count.
                n_re_native = sum(1 for c, s in known_fs if c == "re-executable" and s == "field")
                n_re_proxy  = sum(1 for c, s in known_fs if c == "re-executable" and s != "field")
                n_fr_native = sum(1 for c, s in known_fs if c == "frozen" and s == "field")
                n_fr_proxy  = sum(1 for c, s in known_fs if c == "frozen" and s != "field")
                n_re = n_re_native + n_re_proxy  # total re-executable for ⚠⚠ predicate
                n_fr = n_fr_native + n_fr_proxy
                n_unknown = n_leaves - len(known_fs)
                fs_parts = []
                if n_re_native:
                    fs_parts.append(f"{n_re_native}/{n_leaves} re-executable")
                if n_re_proxy:
                    fs_parts.append(f"{n_re_proxy}/{n_leaves} re-executable-leaning (proxy:quote_type)")
                if n_fr_native:
                    fs_parts.append(f"{n_fr_native}/{n_leaves} frozen")
                if n_fr_proxy:
                    fs_parts.append(f"{n_fr_proxy}/{n_leaves} frozen-leaning (proxy:quote_type)")
                if n_unknown:
                    fs_parts.append(f"{n_unknown}/{n_leaves} unknown")
                warnings.append(f"FALSIFICATION: {'; '.join(fs_parts)}.")

                # ⚠⚠ composite escalation (v3 §4.1): lineage-uniform AND no leaf
                # re-testable. Reads lineage_uniform from the STANDPOINT block —
                # it is False unless that block ran AND found uniformity, so ⚠⚠
                # stays gated on standpoint data being present. Honesty floor:
                # fires only when ALL leaves have a KNOWN fs_class (no
                # true-by-ignorance escalation). [Borges folds the verified-proxy
                # here: retarget the `n_re == 0` predicate to "zero state-3 /
                # verified leaves".]
                # n_verified retarget (Component D, Borges #721): predicate is
                # "zero state-3 (reality-VERIFIED) leaf", not "zero re-executable"
                # (state-2). Per Lei's verifiable≠verified axis a
                # re-executable-but-unrun leaf does NOT discharge. The old
                # n_unknown==0 floor is dropped — _leaf_verified is fully
                # determinate (no unknown bucket; Luria #723).
                n_verified = sum(1 for leaf in obs_leaves if _leaf_verified(conn, leaf))
                if lineage_uniform and n_verified == 0:
                    proxy_suffix = (
                        " [F-S via quote_type-proxy — Phase-2 field may revise]"
                        if any_proxy else ""
                    )
                    # Discharge condition is a VERIFIED (state-3) premise, not
                    # merely re-executable. (Borges owns the fuller own-vs-foreign
                    # remedy menu: own/unverified → only "anchor it (verify a
                    # premise)" resolves; "diversify standpoint" does not anchor
                    # your own claim.)
                    if _graph_lineage_count(conn) >= 2:
                        remedy = (
                            "treat as single-witness until a reality-verified "
                            "premise or cross-lineage support is added"
                        )
                    else:
                        remedy = (
                            "treat as single-witness until a reality-verified "
                            "premise is added (re-executable-but-unverified is "
                            "not enough)"
                        )
                    warnings.append(
                        f"⚠⚠ unverified + uniform: no premise is reality-verified "
                        f"and all share training lineage — corroboration on this "
                        f"derivation cannot be improved by re-checking; "
                        f"{remedy}{proxy_suffix}."
                    )

        # Role-axis prompt (issue #933): only meaningful for multi-premise
        # derivations — which premise carries the entailment. Gated on n >= 2
        # even though the outer block is now n >= 1 (#958). A contextual weak
        # premise can floor a deduction (under-confidence) or pad a
        # corroboration (over-confidence) with all evidence roots distinct, and
        # every mechanical check above stays silent. Never a pass/fail, never
        # an auto-discount — an auto-discount would need to know which premise
        # is load-bearing, and it can't (forum #19 consensus, three graphs).
        if n >= 2:
            warnings.append(
                "NOT CHECKED (agent judgment): premise role — is the load-bearing "
                "premise the one driving the confidence? Context premises belong "
                "in context_ids."
            )

    return warnings


def _create_derivation(
    conn: sqlite3.Connection,
    *,
    claim: str,
    supporting_ids: list,
    logical_chain: str,
    reasoning_type: str,
    context_ids: Optional[list] = None,
    use_stale: bool = False,
    use_contested: bool = False,
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
        use_contested: MECH-5 opt-in for premises under an open,
            unresolved contradiction (#1654) — the resolution-pressure
            valve. metadata.built_on_contested is auto-stamped by this
            path only, never author-supplied.
        extra_meta: merged into derivation metadata after reasoning
            fields — use for resolve-specific keys (resolves,
            resolution_status)
        history_reason: optional confidence_history reason string;
            defaults to a generic "Derived (reasoning_type, ...)" line

    Returns:
        (block, success) — exactly one is non-None.
          block: MECH-5 guard response ready for json.dumps when a
            premise was tainted, contradicted, or stale without opt-in
          success: dict with node_id, confidence, reasoning_type,
            reasoning_class, stale_ids, contested_ids, structure_warnings,
            bumped_count, context_nodes
    """
    block, stale_ids, contested_ids = _validate_premises(
        supporting_ids, conn, use_stale=use_stale, use_contested=use_contested,
    )
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
    if use_contested and contested_ids:
        derive_meta["built_on_contested"] = contested_ids
    if extra_meta:
        # By-CONSTRUCTION guarantee, not by-convention (Borges's #1711 review):
        # these two audit stamps are auto-written by their override paths
        # ONLY and must never be spoofable/removable via extra_meta, even if
        # a future caller gains a generic extra_meta/metadata passthrough.
        # No caller does today (extra_meta is hardcoded to {"warrant": ...}
        # at the one call site), but that's an accident of today's callers,
        # not a structural guarantee — strip the reserved keys unconditionally
        # rather than relying on no one ever setting them.
        safe_extra_meta = {
            k: v for k, v in extra_meta.items()
            if k not in ("built_on_stale", "built_on_contested")
        }
        derive_meta.update(safe_extra_meta)

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
        "contested_ids": contested_ids,
        "structure_warnings": structure_warnings,
        "bumped_count": bumped_count,
        "context_nodes": ctx_used,
    })
