"""engram_core — shared state, DB layer, and write primitives for the ENGRAM MCP server.

Extracted from server.py in #872 wave 1. HOUSE RULES (spec D3):
- Mutable module state (path globals set by _configure_paths; runtime flags)
  must be accessed via `import engram_core as core; core.NAME` — NEVER via
  `from engram_core import NAME` (name-binding holds a stale copy after
  _configure_paths / runtime flips).
- This module must not import server.py or any family module (acyclic).
- Helpers used by 3+ tool families get promoted here in the family wave that
  first needs them out of server.py (rolling promotion; spec D6 note).
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

from engram_confidence import CONFIDENCE_MAP, PREDICTIVE_CONFIDENCE_CAP  # noqa: E402


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
    global FEELING_NUDGE_MARKER, ERROR_INCIDENTS_PATH
    global _walguard_last_check, _walguard_startup_done, _walguard_disabled_logged
    DATA_DIR = Path(data_dir)
    DB_PATH = DATA_DIR / "knowledge.db"
    SNAPSHOT_PATH = DATA_DIR / "graph_snapshot.md"
    CONFIG_PATH = DATA_DIR / "config.json"
    LOG_PATH = DATA_DIR / "session_log.md"
    FEELING_NUDGE_MARKER = DATA_DIR / "feeling-nudge-active.json"
    ERROR_INCIDENTS_PATH = str(DATA_DIR / "error_incidents.json")
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
    "resolves": {
        "cascade": False,      # not a confidence-propagating edge
        "dag_check": True,
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
        "dag_check": True,
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
        "dag_check": True,
        "removable": True,
        "addable_after_creation": True,
        "provenance": False,
    },
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
                # no auto-offset (see tools/migration/migrate_config_v2.py header).
                "heartbeat_minutes": [0, 30],
                "sleep_hour": 20,
                "sleep_minute": 30,
                "sleep_protected_window_minutes": 60,
                "drowsiness_caution_pct": 80,
                "drowsiness_urgent_pct": 90,
                # Auto-sleep scheduler — consumed by SessionStart hook.
                # Registers a nightly sleep cron at session start when enabled.
                # Changes take effect next session (restart_required).
                "auto_sleep_enabled": False,
                "auto_sleep_time": "03:00",
            },
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
        # Retract trigger (#274, round-2 #280): remove retracted nodes from FTS.
        # Fires on AFTER UPDATE OF status when status transitions INTO 'retracted',
        # regardless of whether the node was current or already-superseded at the
        # time of retraction. The COALESCE guard prevents double-delete on idempotent
        # re-retracts (FTS5 'delete' magic-insert is NOT idempotent — double-delete
        # raises "database disk image is malformed").
        #
        # Round-1 used OLD.is_current=1, which missed the supersede-then-retract
        # path: if a node was superseded first (is_current→0) and then retracted,
        # the trigger did not fire and the node leaked through FTS queries with
        # include_superseded=True. Fixed by keying on status transition instead.
        #
        # Superseded nodes are intentionally NOT removed — they're past memories,
        # still recallable via include_superseded=True (Lei's design call, 2026-05-22).
        # FTS5 contentless-table mechanic: the 'delete' magic-insert requires
        # OLD column values because the contentless table doesn't store them.
        """
        CREATE TRIGGER IF NOT EXISTS nodes_retract_remove_from_fts
        AFTER UPDATE OF status ON nodes
        WHEN NEW.status = 'retracted' AND COALESCE(OLD.status, '') != 'retracted'
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
            VALUES ('delete', OLD.rowid,
                    COALESCE(OLD.claim, ''),
                    COALESCE(OLD.quoted_text, ''),
                    COALESCE(OLD.interpretation, ''));
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
    # Superseded nodes are deliberately left in the index (Lei's design call).
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


def _commit_snapshot(
    conn: sqlite3.Connection,
    message: str,
    mode: str,
    skip_checkpoint: bool = False,
) -> dict:
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

    SQLite WAL is checkpointed (TRUNCATE) before generating the SQL dump,
    unless skip_checkpoint=True or a degraded marker is present — checkpointing
    under a displaced WAL-index is the corruption vector (#786); the SQL dump
    reads through the coherent connection and is safe without a prior checkpoint.

    Args:
        conn: Live database connection.
        message: Commit message body.
        mode: Commit mode tag (e.g. "nap", "sleep", "emergency").
        skip_checkpoint: When True, omit the PRAGMA wal_checkpoint(TRUNCATE)
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

    # 2. Flush WAL into the main db file so the committed db is consistent.
    # Skipped when skip_checkpoint=True: checkpointing under a displaced WAL-index
    # is the corruption vector (#786); the SQL dump reads through the coherent
    # connection and is safe.
    wal_warning = None
    if not skip_checkpoint:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            # Non-fatal — continue, but record it
            wal_warning = f"wal_checkpoint failed: {e}"

    # 2b. Generate embedding-stripped SQL text dump (the SQL-dump-replaces-binary derivation).
    #     Embeddings (~384-float blobs, ~37 MB) are regenerable from claim text and must
    #     NOT be included — they churn on every nap and defeat delta compression, growing
    #     .git to GBs (verified 2026-06-02: 79.7 MB → 42.6 MB stripped, lossless rebuild).
    #     Method: engram_backup.dump_stripped (pure-Python, no sqlite3 CLI).
    #
    #     Restore path: `sqlite3 ~/.engram/knowledge.db < knowledge.sql`
    #     After restore: run `python tools/engram-regenerate-embeddings.py` to rebuild
    #     semantic search (embedding column is NULL in the dump, NULL in the restored DB).
    sql_dump_path = DATA_DIR / "knowledge.sql"
    _dump_stats = None
    _dump_error = None
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

    # Collect dump fields once so every subsequent return path carries them.
    # The agent-visible response must surface sql_dump_error on ALL exit paths,
    # not only the success path — otherwise a backup failure is visible on stderr
    # but invisible to the agent reading the nap/sleep response (the class-5
    # silent-failure mode this PR eliminates).
    _dump_fields: dict = {}
    if _dump_stats is not None:
        _dump_fields["sql_dump"] = _dump_stats
    if _dump_error is not None:
        _dump_fields["sql_dump_error"] = _dump_error

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

