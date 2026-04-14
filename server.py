"""
Protocol-Governed Knowledge Graph Memory Server
================================================
An MCP server that provides structured knowledge graph operations
for LLM agents, implementing epistemic hierarchy principles:
Evidence → Observation → Derivation, with full provenance tracking,
confidence propagation, and version control via git checkpoints.

MVP tools: ingestion (add_evidence, add_observation, check_similar),
query (query, get_subgraph), version control (checkpoint, diff).
"""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastmcp import FastMCP


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

# Confidence defaults by quote type
CONFIDENCE_MAP = {
    "hard_data": 0.95,
    "official_statement": 0.85,
    "attributed_analysis": 0.70,
    "unnamed_source": 0.50,
    "editorial": 0.35,
}

VALID_QUOTE_TYPES = set(CONFIDENCE_MAP.keys())

VALID_SOURCE_CLASSES = {"external", "introspective", "user_stated"}
SOURCE_CLASS_CONFIDENCE_DISCOUNT = {
    "external": 1.0,        # Standard — no adjustment
    "introspective": 0.95,  # Agent's own prior output — slight discount
    "user_stated": None,    # Override: treated as official_statement regardless of quote_type
}

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
}

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
}

PREDICTIVE_CONFIDENCE_CAP = 0.60
CONJECTURE_CONFIDENCE_DEFAULT = 0.40
CONJECTURE_CONFIDENCE_MIN = 0.10
CONJECTURE_CONFIDENCE_MAX = 0.60

# ---------------------------------------------------------------------------
# Reasoning types for derivations (v2)
# ---------------------------------------------------------------------------

REASONING_TYPES = {
    # Deductive: if premises are true, conclusion MUST be true
    "deductive_modus_ponens",       # If P then Q; P; therefore Q
    "deductive_modus_tollens",      # If P then Q; not Q; therefore not P
    "deductive_hypothetical_syllogism",  # If P→Q and Q→R; therefore P→R
    "deductive_disjunctive",        # P or Q; not P; therefore Q
    "deductive_reductio",           # Assume P; derive contradiction; therefore not P
    # Inductive: more evidence = more likely, but never certain
    "inductive_generalization",     # Observed X in A,B,C → X is generally true
    "inductive_statistical",        # X% of A are B; this is A; therefore probably B
    "inductive_analogy",            # A~B in X,Y,Z; A has W; therefore B has W
    "inductive_causal",             # A precedes B + mechanism → A causes B
    # Abductive: best available explanation, alternatives may exist
    "abductive_best_explanation",   # Observations → hypothesis that best explains them
    "abductive_elimination",        # Hypotheses H1,H2,H3; rule out H1,H2 → H3
    # Authority: trust transfer, not logical reasoning
    "authority_expert",             # Expert X says P → P is probably true
    "authority_consensus",          # Multiple independent sources agree → probably true
}

# Class-level grouping for confidence computation
REASONING_CLASS = {
    "deductive_modus_ponens": "deductive",
    "deductive_modus_tollens": "deductive",
    "deductive_hypothetical_syllogism": "deductive",
    "deductive_disjunctive": "deductive",
    "deductive_reductio": "deductive",
    "inductive_generalization": "inductive_corroboration",
    "inductive_statistical": "inductive_chain",
    "inductive_analogy": "inductive_chain",
    "inductive_causal": "inductive_chain",
    "abductive_best_explanation": "abductive",
    "abductive_elimination": "abductive",
    "authority_expert": "authority",
    "authority_consensus": "inductive_corroboration",
}

# Per-type confidence discount applied to the reasoning step itself
REASONING_DISCOUNT = {
    "deductive_modus_ponens": 0.98,
    "deductive_modus_tollens": 0.98,
    "deductive_hypothetical_syllogism": 0.98,
    "deductive_disjunctive": 0.98,
    "deductive_reductio": 0.98,
    "inductive_generalization": 0.95,
    "inductive_statistical": 0.90,
    "inductive_analogy": 0.70,
    "inductive_causal": 0.85,
    "abductive_best_explanation": 0.80,
    "abductive_elimination": 0.88,
    "authority_expert": 0.95,
    "authority_consensus": 0.98,
}

# Abductive confidence caps — even with perfect premises, abductive
# reasoning can't exceed these ceilings
ABDUCTIVE_CONFIDENCE_CAP = {
    "abductive_best_explanation": 0.80,
    "abductive_elimination": 0.90,
}

# Structural expectations per reasoning type — used for validation warnings
REASONING_STRUCTURE = {
    "deductive_modus_ponens": {"min_premises": 2, "max_premises": 2, "multi_source": False},
    "deductive_modus_tollens": {"min_premises": 2, "max_premises": 2, "multi_source": False},
    "deductive_hypothetical_syllogism": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "deductive_disjunctive": {"min_premises": 2, "max_premises": None, "multi_source": False},
    "deductive_reductio": {"min_premises": 1, "max_premises": None, "multi_source": False},
    "inductive_generalization": {"min_premises": 2, "max_premises": None, "multi_source": True},
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
    stays False and engram_checkpoint silently skips committing while still
    saving the session log and graph state to disk.
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


def _get_db() -> sqlite3.Connection:
    """Get a database connection, creating tables if needed."""
    _ensure_data_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

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

        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
        CREATE INDEX IF NOT EXISTS idx_nodes_current ON nodes(is_current);
        CREATE INDEX IF NOT EXISTS idx_nodes_evidence_id ON nodes(evidence_id);
    """
    )

    # Migrate existing DBs: add memory columns if missing
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    for col, typedef in [
        ("importance_base", "REAL DEFAULT 0.5"),
        ("importance_score", "REAL DEFAULT 0.5"),
        ("recall_turn", "INTEGER DEFAULT 0"),
        ("recall_count", "INTEGER DEFAULT 0"),
        ("memory_status", "TEXT DEFAULT 'active'"),
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
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typedef}")

    # One-time backfill: classify any evidence rows that still have NULL source_type
    # using URL pattern rules. Idempotent: re-runs are no-ops once all rows are filled.
    _backfill_source_type(conn)

    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_importance ON nodes(importance_score)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_memory_status ON nodes(memory_status)")
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

        CREATE INDEX IF NOT EXISTS idx_edit_history_node
            ON edit_history(node_id);
        CREATE INDEX IF NOT EXISTS idx_edit_history_ts
            ON edit_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_edit_history_action
            ON edit_history(action);
        CREATE INDEX IF NOT EXISTS idx_diagnostic_history_turn
            ON diagnostic_history(turn);
    """)

    # One-time backfill: populate edit_history from existing nodes if empty.
    _backfill_edit_history(conn)

    conn.commit()

    # DAG invariant check: source (dependent) should be created at or after
    # target (dependency). Log warnings for violations (may exist from legacy
    # reroute operations).
    try:
        violations = conn.execute("""
            SELECT e.source_id, e.target_id, e.relation,
                   s.created_at as src_time, t.created_at as tgt_time
            FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE s.created_at < t.created_at
              AND e.relation NOT IN ('contradicts')
            LIMIT 5
        """).fetchall()
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
        os.path.expanduser("~/kg_protocol_for_ai"),
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
    Tier 2 (searchable): used by query, check_similar
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
    """Compute embedding for a node and store it. Silently skips if unavailable."""
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
    except Exception:
        pass  # Embedding computation failed — node works fine without it


def _semantic_search(
    conn: sqlite3.Connection,
    query_text: str,
    top_k: int = 10,
    min_similarity: float = 0.3,
    type_filter: set = None,
    importance_threshold: float = 0.0,
) -> list[dict]:
    """Search nodes by embedding similarity.
    
    Returns list of {"id", "type", "claim", "similarity", ...} sorted by similarity.
    Falls back to empty list if embeddings are unavailable or any error occurs.
    """
    try:
        emb_config = _get_embedding_config()
        if not emb_config.get("enabled", True) or not _embedder.is_available():
            return []

        model_name = emb_config.get("model", DEFAULT_EMBEDDING_MODEL)
        query_vector = _embedder.embed(query_text, model_name)
        if not query_vector:
            return []

        # Fetch all candidate nodes with embeddings
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
    """Generate the next prefixed ID for a given node type."""
    prefix = TYPE_PREFIX.get(node_type, "nd")
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM nodes WHERE id LIKE ?", (f"{prefix}_%",)
    ).fetchone()
    num = (row["cnt"] if row else 0) + 1
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
               n.confidence as neighbor_confidence, n.is_current as neighbor_is_current
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
        result.append(
            {
                "relation": e["relation"],
                "direction": direction,
                "neighbor_id": neighbor_id,
                "neighbor_type": e["neighbor_type"],
                "neighbor_claim": e["neighbor_claim"],
                "neighbor_confidence": e["neighbor_confidence"],
            }
        )
    return result


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
        "Do not paraphrase, summarize, or fabricate quotes."
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
    ]
    type_labels = {
        "evidence": "Evidence",
        "axiom": "Axioms",
        "definition": "Definitions",
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
      - knowledge.db        — the actual data (binary, but enables true restore)
      - session_log.md      — chronological session audit trail
      - config.json         — trust pool / confidence configuration

    SQLite WAL is checkpointed (TRUNCATE) before staging knowledge.db so the
    committed file contains all writes from this session.
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

    # 3. Stage explicit files only (never `git add .` — would pull in backups,
    #    daemon sockets, etc.)
    files_to_stage = []
    for fname in ("graph_snapshot.md", "knowledge.db", "session_log.md", "config.json"):
        if (DATA_DIR / fname).exists():
            files_to_stage.append(fname)

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
    return result


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("engram_mcp")


# ---- Ingestion tools ----


# Internal helper — not exposed as an MCP tool.
# Called by engram_add_observation and engram_add_observation_batch to auto-create evidence nodes.
def engram_add_evidence(
    url: str,
    title: str,
    domain: str = "",
    source_date: str = "",
    content_snippet: str = "",
    content_hash: str = "",
    git_sha: str = "",
) -> str:
    """Register a new source document (webpage, article, data release) in the knowledge graph.

    Creates an Evidence node representing the raw source material. Evidence nodes
    are immutable references — they record that a specific source exists, not what
    it means. Observations are extracted from evidence in a separate step.

    For web sources: if the URL already exists, returns the existing node ID.
    For file-based sources (file:// URLs): if the URL exists but content_hash
    differs, creates a new version (the file changed since last read). If the
    content_hash matches, returns the existing node (file unchanged).

    Args:
        url: Canonical URL of the source document. For local files, use
             'file://path/to/file' format.
        title: Article or page title.
        domain: Source domain (e.g. 'reuters.com'). Auto-extracted from URL if omitted.
        source_date: Publication or byline date of the source (ISO format, e.g. '2026-03-20').
                     Extract from the article's dateline or byline when available.
        content_snippet: Optional truncated excerpt of relevant content for offline re-reading.
        content_hash: SHA-256 hash of the file content. Used for change detection on
                      file-based evidence. If the same URL exists with a different hash,
                      a new evidence version is created.
        git_sha: Git commit SHA at the time this evidence was read. Enables future
                 diffing via 'git diff old_sha..new_sha -- file'.

    Returns:
        JSON with the evidence node ID and trust pool status.
    """
    conn = _get_db()
    try:
        # Check for existing URL
        existing = conn.execute(
            "SELECT id, metadata FROM nodes WHERE source_url = ? AND type = 'evidence'"
            " ORDER BY created_at DESC",
            (url,)
        ).fetchone()
        if existing:
            # For file-based evidence with content_hash, check if content changed
            if content_hash and url.startswith("file://"):
                existing_meta = json.loads(existing["metadata"] or "{}")
                if existing_meta.get("content_hash") != content_hash:
                    # File changed — create new version (fall through to creation below)
                    pass
                else:
                    # Same content — reuse existing
                    return json.dumps(
                        {
                            "status": "already_exists",
                            "evidence_id": existing["id"],
                            "message": f"Evidence node {existing['id']} already registered (content unchanged).",
                        }
                    )
            else:
                return json.dumps(
                    {
                        "status": "already_exists",
                        "evidence_id": existing["id"],
                        "message": f"Evidence node {existing['id']} already registered for this URL.",
                    }
                )

        # Validate evidence URL resolvability
        RECOGNIZED_SCHEMES = ("http://", "https://", "file://")
        _HONESTY_REMINDER = (
            "\n\nRemember: cheating blinds you. A fabricated URL corrupts your own memory "
            "and every future step built on it. If the real source is hard to find, "
            "raise the problem transparently — never solve it with a shortcut. (ax_0002)"
        )
        if url.startswith("file://"):
            file_path = url[7:]  # Strip file:// prefix
            if not os.path.exists(file_path):
                return json.dumps({
                    "error": f"File not found: '{file_path}'. Evidence URLs with file:// must point to existing files. "
                    "If citing a conversation, use the JSONL transcript file path instead."
                    + _HONESTY_REMINDER
                })

            # --- Git versioning enforcement for file-based evidence ---
            # Determine file's git repo (if any)
            file_dir = os.path.dirname(os.path.abspath(file_path))
            _file_git = lambda *args: subprocess.run(
                [GIT_EXE, *args], cwd=file_dir, capture_output=True, text=True, timeout=GIT_TIMEOUT
            )
            try:
                repo_check = _file_git("rev-parse", "--is-inside-work-tree")
                file_in_git_repo = repo_check.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                file_in_git_repo = False

            if file_in_git_repo:
                # Get repo-relative path for git commands
                repo_root_result = _file_git("rev-parse", "--show-toplevel")
                repo_root = repo_root_result.stdout.strip()
                abs_file = os.path.abspath(file_path)
                rel_path = os.path.relpath(abs_file, repo_root)

                # Check if file is committed (tracked and clean)
                status_result = _file_git("status", "--porcelain", "--", rel_path)
                file_status = status_result.stdout.strip()
                if file_status:
                    # File has uncommitted changes — determine the kind
                    status_code = file_status[:2]
                    if status_code == "??":
                        status_desc = "untracked (not yet added to git)"
                    elif "M" in status_code:
                        status_desc = "modified but not committed"
                    elif "A" in status_code:
                        status_desc = "staged but not committed"
                    else:
                        status_desc = f"uncommitted (status: {status_code.strip()})"
                    return json.dumps({
                        "error": f"File is {status_desc}: '{rel_path}'. "
                        "File-based evidence must be committed to git before it can be cited. "
                        "This ensures the evidence is verifiable at the claimed revision. "
                        "Commit the file first, then retry with the new git SHA."
                        + _HONESTY_REMINDER
                    })

                # Auto-compute content_hash if not provided
                if not content_hash:
                    with open(abs_file, "rb") as f:
                        content_hash = hashlib.sha256(f.read()).hexdigest()

                # Auto-populate git_sha from HEAD if not provided
                if not git_sha:
                    head_result = _file_git("rev-parse", "HEAD")
                    if head_result.returncode == 0:
                        git_sha = head_result.stdout.strip()

                # If git_sha is provided, verify the file exists at that revision
                if git_sha:
                    verify_result = _file_git("cat-file", "-e", f"{git_sha}:{rel_path}")
                    if verify_result.returncode != 0:
                        return json.dumps({
                            "error": f"File '{rel_path}' does not exist at git revision {git_sha[:8]}. "
                            "The git_sha must point to a commit that contains this file. "
                            "Check that you committed the file before this revision."
                            + _HONESTY_REMINDER
                        })
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

        if not domain:
            domain = _extract_domain(url)

        # Check trust pool
        config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        trust_pool = config.get("trust_pool", [])
        in_trust_pool = any(domain.endswith(tp) for tp in trust_pool)

        node_id = _next_id(conn, "evidence")
        now = _now()

        # Build metadata for file-based evidence
        ev_metadata = {}
        prior_evidence_id = None
        if content_hash:
            ev_metadata["content_hash"] = content_hash
        if git_sha:
            ev_metadata["git_sha"] = git_sha
        if existing and content_hash and url.startswith("file://"):
            # This is a new version of a changed file
            prior_evidence_id = existing["id"]
            ev_metadata["prior_evidence_id"] = prior_evidence_id

        # Auto-tag source_type from URL pattern (orthogonal to source_class).
        # Used by the dedup heuristic to grade action_hints by artifact kind.
        source_type = _infer_source_type(url)

        conn.execute(
            """INSERT INTO nodes (id, type, created_at, source_url, source_title,
               source_domain, source_date, source_accessed, content_snippet, metadata,
               source_type)
               VALUES (?, 'evidence', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, now, url, title, domain, source_date or None, now,
             content_snippet or None, json.dumps(ev_metadata) if ev_metadata else "{}",
             source_type),
        )

        # If this is a new version, add supersedes edge to prior version
        if prior_evidence_id:
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supersedes', ?)",
                    (node_id, prior_evidence_id, now),
                )
            except sqlite3.IntegrityError:
                pass

        _stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        conn.commit()

        result = {
            "status": "created",
            "evidence_id": node_id,
            "domain": domain,
            "source_date": source_date or None,
            "in_trust_pool": in_trust_pool,
        }
        if prior_evidence_id:
            result["prior_evidence_id"] = prior_evidence_id
            result["message"] = f"New version of {prior_evidence_id} (content changed)."
        if not in_trust_pool:
            result["warning"] = (
                f"Domain '{domain}' is NOT in the trusted source pool. "
                "Consider whether this source meets your quality standards."
            )
        return json.dumps(result)
    finally:
        conn.close()


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
def engram_add_observation(
    quoted_text: str,
    interpretation: str,
    claim: str,
    quote_type: str,
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
    """Extract and record a claim from a source document with full provenance.

    Creates an Observation node representing the agent's interpretation of a
    specific passage from an evidence source. Every observation has three parts:
    the exact quoted text, the agent's interpretation, and the atomic claim.

    Source identification: provide EITHER url+title (the evidence node will be
    auto-created or reused if the URL already exists) OR evidence_id (to cite
    an already-registered evidence node). If both are provided, evidence_id
    takes precedence.

    If is_predictive is True, also creates or links to a Prediction node,
    decomposing the forward-looking claim into a factual observation (who
    predicted it) plus a prediction target (what was predicted).

    Args:
        quoted_text: Exact quote from the source document.
        interpretation: Agent's reasoning about what this quote means.
        claim: The atomic, falsifiable claim this observation supports.
        quote_type: One of: hard_data, official_statement, attributed_analysis, unnamed_source, editorial.
        url: URL of the source document. The evidence node is auto-created or reused.
        title: Title of the source document (required if url is provided).
        domain: Source domain (auto-extracted from URL if omitted).
        source_date: Publication date in ISO format (e.g. '2026-03-20').
        evidence_id: ID of an existing evidence node. Use this OR url+title, not both.
        is_predictive: Whether this observation records a forward-looking prediction.
        predicted_event: Required if is_predictive. The event being predicted.
        resolution_timeframe: Optional timeframe for when the prediction should be resolvable.
        source_class: Epistemic origin — 'external' (published sources, default), 'introspective' (agent's own prior output, ×0.95 confidence discount), or 'user_stated' (user-provided facts, treated as official_statement confidence).
        content_hash: SHA-256 hash of file content (for file-based evidence). Enables change detection across consolidation cycles.
        git_sha: Git commit SHA when the file was read (for file-based evidence). Enables future diffing.

    Returns:
        JSON with the new observation ID, confidence, evidence ID, and any prediction node created.
    """
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

    if not evidence_id and url:
        # Auto-create or reuse evidence node
        ev_result = json.loads(engram_add_evidence(
            url=url, title=title, domain=domain, source_date=source_date,
            content_hash=content_hash, git_sha=git_sha,
        ))
        if ev_result.get("error"):
            return json.dumps({"error": f"Failed to create evidence node: {ev_result['error']}"})
        evidence_id = ev_result["evidence_id"]

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

        node_type = "observation_predictive" if is_predictive else "observation_factual"
        confidence = _compute_confidence(
            conn, node_type, quote_type=quote_type, is_predictive=is_predictive,
            source_class=source_class,
        )

        # Inline similarity check — run BEFORE insert so the new node
        # doesn't match itself.  Results are advisory (non-blocking).
        similar_matches = []
        try:
            tier2_threshold = _get_tier_threshold(conn, 2)

            # FTS keyword search
            fts_ids = set()
            try:
                fts_results = conn.execute(
                    """SELECT n.id, n.type, n.claim, n.confidence, n.evidence_id,
                              n.is_current, n.importance_score
                       FROM nodes_fts fts
                       JOIN nodes n ON n.rowid = fts.rowid
                       WHERE nodes_fts MATCH ? AND n.is_current = 1
                       AND n.type IN ('observation_factual', 'observation_predictive')
                       AND COALESCE(n.importance_score, 0) >= ?
                       ORDER BY rank
                       LIMIT 5""",
                    (claim, tier2_threshold),
                ).fetchall()
                for r in fts_results:
                    similar_matches.append({
                        "id": r["id"],
                        "claim": r["claim"],
                        "confidence": r["confidence"],
                        "evidence_id": r["evidence_id"],
                        "match_type": "keyword",
                    })
                    fts_ids.add(r["id"])
            except sqlite3.OperationalError:
                pass  # FTS query syntax error (special chars in claim)

            # Semantic search (complement)
            if _embedder.is_available() and _get_embedding_config().get("enabled", True):
                sem_results = _semantic_search(
                    conn, claim, top_k=5,
                    min_similarity=0.5,
                    importance_threshold=tier2_threshold,
                )
                for s in sem_results:
                    if s["id"] not in fts_ids and s["type"] in (
                        "observation_factual", "observation_predictive"
                    ):
                        ev_row = conn.execute(
                            "SELECT evidence_id FROM nodes WHERE id = ?", (s["id"],)
                        ).fetchone()
                        similar_matches.append({
                            "id": s["id"],
                            "claim": s["claim"],
                            "confidence": s["confidence"],
                            "evidence_id": ev_row["evidence_id"] if ev_row else None,
                            "match_type": "semantic",
                            "similarity": s["similarity"],
                        })
        except Exception:
            pass  # Similarity check is best-effort; never block creation

        node_id = _next_id(conn, node_type)
        now = _now()
        metadata = json.dumps({"source_class": source_class})
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
                    if sim >= 0.80:
                        m["action_hint"] = (
                            f"CORROBORATE — high-similarity ({sim:.2f}) claim from an independent source. "
                            f"Create a corroboration derivation (inductive_generalization) "
                            f"citing both {m['id']} and {node_id}."
                        )
                    elif sim >= 0.60:
                        m["action_hint"] = (
                            f"RELATED — moderate similarity ({sim:.2f}) from a different source. "
                            f"Inspect to decide whether corroboration is warranted."
                        )
                    else:
                        m["action_hint"] = (
                            f"WEAK_MATCH — low similarity ({sim:.2f}). "
                            f"Probably unrelated. Ignore unless inspection suggests otherwise."
                        )

            result["similar_existing"] = similar_matches
            result["similar_count"] = len(similar_matches)
            result["dedup_guidance"] = (
                "Similarity-graded matches. Action hints account for source type "
                "(document vs conversation/file) and similarity score. "
                "DUPLICATE only fires for high-similarity matches in discrete documents."
            )

        conn.commit()
        return json.dumps(result)
    finally:
        conn.close()


# ---- Batch ingestion tools ----


# Internal helper — not exposed as an MCP tool.
# Kept for potential programmatic use but agents should use engram_add_observation_batch.
def engram_add_evidence_batch(sources_json: str) -> str:
    """Register multiple source documents in a single call.

    Use this when you have several sources to register at once — saves tool
    call round trips compared to calling engram_add_evidence individually.

    Args:
        sources_json: JSON array of source objects, each with:
            - url (required): URL of the source
            - title (required): Title or headline
            - domain (optional): Domain name, auto-extracted from URL if omitted
            - source_date (optional): Publication date in ISO format (e.g. '2026-03-20')
            - content_snippet (optional): Brief excerpt

    Example:
        sources_json = '[{"url": "https://reuters.com/a", "title": "Article A", "source_date": "2026-03-20"}, {"url": "https://ft.com/b", "title": "Article B"}]'

    Returns:
        JSON with list of created evidence nodes and their trust pool status.
    """
    try:
        sources = json.loads(sources_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(sources, list):
        return json.dumps({"error": "sources_json must be a JSON array."})

    results = []
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            results.append({"index": i, "error": "Each source must be a JSON object."})
            continue
        url = src.get("url", "")
        title = src.get("title", "")
        if not url or not title:
            results.append({"index": i, "error": "Both 'url' and 'title' are required."})
            continue

        r = json.loads(engram_add_evidence(
            url=url,
            title=title,
            domain=src.get("domain", ""),
            source_date=src.get("source_date", ""),
            content_snippet=src.get("content_snippet", ""),
        ))
        r["index"] = i
        results.append(r)

    created = [r for r in results if r.get("status") == "created"]
    return json.dumps({
        "status": "batch_complete",
        "total": len(sources),
        "created": len(created),
        "results": results,
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
def engram_add_observation_batch(
    observations_json: str,
    url: str = "",
    title: str = "",
    domain: str = "",
    source_date: str = "",
    evidence_id: str = "",
    content_hash: str = "",
    git_sha: str = "",
) -> str:
    """Extract multiple observations from a single source in one call.

    Use this after reading a source holistically — extract ALL substantive
    claims at once rather than calling engram_add_observation repeatedly.
    All observations in the batch cite the same source.

    Source identification: provide EITHER url+title (the evidence node will be
    auto-created or reused) OR evidence_id. If both are provided, evidence_id
    takes precedence.

    Args:
        observations_json: JSON array of observation objects, each with:
            - quoted_text (required): Verbatim quote from the source
            - interpretation (required): Your reasoning about what this means
            - claim (required): Atomic, falsifiable claim
            - quote_type (required): hard_data, official_statement, attributed_analysis, unnamed_source, or editorial
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
    if not resolved_evidence_id and url:
        if not title:
            return json.dumps({"error": "'title' is required when providing 'url'."})
        ev_result = json.loads(engram_add_evidence(
            url=url, title=title, domain=domain, source_date=source_date,
            content_hash=content_hash, git_sha=git_sha,
        ))
        if ev_result.get("error"):
            return json.dumps({"error": f"Failed to create evidence node: {ev_result['error']}"})
        resolved_evidence_id = ev_result["evidence_id"]
        evidence_created = ev_result["status"] == "created"
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

        r = json.loads(engram_add_observation(
            evidence_id=resolved_evidence_id,
            quoted_text=obs["quoted_text"],
            interpretation=obs["interpretation"],
            claim=obs["claim"],
            quote_type=obs["quote_type"],
            is_predictive=obs.get("is_predictive", False),
            predicted_event=obs.get("predicted_event", ""),
            resolution_timeframe=obs.get("resolution_timeframe", ""),
            source_class=obs.get("source_class", "external"),
        ))
        r["index"] = i
        results.append(r)

    created = [r for r in results if r.get("status") == "created"]
    return json.dumps({
        "status": "batch_complete",
        "evidence_id": resolved_evidence_id,
        "evidence_created": evidence_created,
        "total": len(observations),
        "created": len(created),
        "results": results,
    })


@mcp.tool(
    name="engram_check_similar",
    annotations={
        "title": "Check for Similar Existing Claims",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_check_similar(claim_text: str, top_k: int = 5) -> str:
    """Search for existing nodes with claims similar to the given text.

    Combines keyword matching (FTS5) with semantic similarity (embeddings)
    to catch both exact and conceptual duplicates. Use this BEFORE creating
    new observations or derivations to check whether a similar claim exists.

    Args:
        claim_text: The candidate claim to check against existing nodes.
        top_k: Maximum number of results to return (default 5).

    Returns:
        JSON list of similar existing nodes with IDs, claims, confidence, support count, and match type.
    """
    conn = _get_db()
    try:
        tier2_threshold = _get_tier_threshold(conn, 2)

        # --- FTS keyword search ---
        fts_ids = set()
        matches = []
        try:
            fts_results = conn.execute(
                """SELECT n.id, n.type, n.claim, n.confidence, n.is_current,
                          n.quote_type, n.created_at, n.importance_score
                   FROM nodes_fts fts
                   JOIN nodes n ON n.rowid = fts.rowid
                   WHERE nodes_fts MATCH ? AND n.is_current = 1
                   AND COALESCE(n.importance_score, 0) >= ?
                   ORDER BY rank
                   LIMIT ?""",
                (claim_text, tier2_threshold, top_k),
            ).fetchall()

            for r in fts_results:
                support_count = conn.execute(
                    "SELECT COUNT(*) as c FROM edges WHERE target_id = ? AND relation IN ('supports', 'cites', 'derives_from')",
                    (r["id"],),
                ).fetchone()["c"]

                matches.append({
                    "id": r["id"],
                    "type": r["type"],
                    "claim": r["claim"],
                    "confidence": r["confidence"],
                    "quote_type": r["quote_type"],
                    "support_count": support_count,
                    "created_at": r["created_at"],
                    "match_type": "keyword",
                })
                fts_ids.add(r["id"])
        except sqlite3.OperationalError:
            pass  # FTS query syntax error

        # --- Semantic search (complement) ---
        if _embedder.is_available() and _get_embedding_config().get("enabled", True):
            sem_results = _semantic_search(
                conn, claim_text, top_k=top_k,
                min_similarity=0.5,  # higher threshold for dedup
                importance_threshold=tier2_threshold,
            )
            for s in sem_results:
                if s["id"] not in fts_ids:
                    support_count = conn.execute(
                        "SELECT COUNT(*) as c FROM edges WHERE target_id = ? AND relation IN ('supports', 'cites', 'derives_from')",
                        (s["id"],),
                    ).fetchone()["c"]

                    full = conn.execute(
                        "SELECT created_at FROM nodes WHERE id = ?", (s["id"],)
                    ).fetchone()

                    matches.append({
                        "id": s["id"],
                        "type": s["type"],
                        "claim": s["claim"],
                        "confidence": s["confidence"],
                        "quote_type": s["quote_type"],
                        "support_count": support_count,
                        "created_at": full["created_at"] if full else "",
                        "match_type": "semantic",
                        "similarity": s["similarity"],
                    })

        # Trim to top_k
        matches = matches[:top_k]

        return json.dumps(
            {
                "query": claim_text,
                "matches": matches,
                "count": len(matches),
                "search_methods": {
                    "keyword_matches": len(fts_ids),
                    "semantic_matches": len(matches) - len(fts_ids),
                    "semantic_available": _embedder.is_available(),
                },
                "message": (
                    "No similar claims found. Safe to create a new node."
                    if not matches
                    else f"Found {len(matches)} similar claim(s). Consider adding support to an existing node instead of creating a duplicate."
                ),
            }
        )
    finally:
        conn.close()


# ---- Internal search layer ----


def _search_nodes(
    conn: sqlite3.Connection,
    query: str,
    types: str = "",
    min_confidence: float = 0.0,
    include_superseded: bool = False,
    top_k: int = 10,
    use_semantic: bool = True,
) -> dict:
    """Pure FTS5 + semantic search.  No side effects (no refresh, no neighbor expansion).

    Returns dict with:
        results: list of node dicts (from _node_to_dict) with match_type/similarity added
        keyword_matches: count of FTS hits
        semantic_matches: count of semantic-only hits
        semantic_available: bool
    """
    type_filter = set(t.strip() for t in types.split(",") if t.strip()) if types else None
    tier2_threshold = _get_tier_threshold(conn, 2)

    # --- FTS keyword search ---
    fts_ids: set[str] = set()
    fts_results: list[sqlite3.Row] = []
    try:
        fts_rows = conn.execute(
            """SELECT n.*, fts.rank
               FROM nodes_fts fts
               JOIN nodes n ON n.rowid = fts.rowid
               WHERE nodes_fts MATCH ?
               ORDER BY fts.rank
               LIMIT ?""",
            (query, top_k * 3),
        ).fetchall()

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
            if len(fts_results) >= top_k:
                break
    except sqlite3.OperationalError:
        pass  # FTS query syntax error — fall through to semantic

    # --- Semantic search (complement, not replace) ---
    semantic_only: list[dict] = []
    if use_semantic and _embedder.is_available() and _get_embedding_config().get("enabled", True):
        sem_results = _semantic_search(
            conn, query, top_k=top_k,
            type_filter=type_filter,
            importance_threshold=tier2_threshold,
        )
        for s in sem_results:
            if s["id"] not in fts_ids:
                if s["confidence"] is not None and s["confidence"] < min_confidence:
                    continue
                semantic_only.append(s)

    # --- Merge: FTS first, then semantic-only ---
    output: list[dict] = []
    seen: set[str] = set()

    for r in fts_results:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        node = _node_to_dict(r)
        node["match_type"] = "keyword"
        output.append(node)

    remaining = top_k - len(output)
    for s in semantic_only[:remaining]:
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        full = conn.execute("SELECT * FROM nodes WHERE id = ?", (s["id"],)).fetchone()
        if full:
            node = _node_to_dict(full)
            node["match_type"] = "semantic"
            node["similarity"] = s["similarity"]
            output.append(node)

    return {
        "results": output,
        "keyword_matches": len(fts_results),
        "semantic_matches": len(semantic_only),
        "semantic_available": _embedder.is_available(),
    }


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
) -> str:
    """Shallow recall: search the KG and return a compact summary nudge.

    This is the "this sounds familiar" layer — a quick scan of what the KG
    knows about a topic. Returns node counts by type, highlights of special
    nodes (axioms, contradictions, open questions, conjectures), top claims,
    and age signals. Does NOT refresh memory on matched nodes.

    Use this to decide whether to dig deeper with engram_inspect (single node
    detail) or engram_get_subgraph (full evidence chain).

    Args:
        query: Natural language search query.
        top_k: Maximum nodes to scan (default 10).
        semantic: Whether to include semantic (embedding) search. Set False
            for fast keyword-only recall (useful in hooks where latency
            matters). Default True.

    Returns:
        JSON with compact summary: type counts, special nodes, top claims,
        age range, stale/tainted counts, and matched node IDs for follow-up.
    """
    conn = _get_db()
    try:
        search = _search_nodes(conn, query, top_k=top_k, use_semantic=semantic)
        results = search["results"]

        if not results:
            return json.dumps({
                "query": query,
                "match_count": 0,
                "message": "No matching nodes found in the knowledge graph.",
            })

        # --- Type breakdown ---
        type_counts: dict[str, int] = {}
        for n in results:
            t = n.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        # --- Special nodes (always worth reviewing) ---
        special = []
        for n in results:
            ntype = n.get("type", "")
            if ntype in ("axiom", "contradiction", "question", "conjecture"):
                entry: dict = {
                    "id": n["id"], "type": ntype,
                    "claim": n.get("claim", ""),
                }
                if ntype == "question":
                    entry["status"] = n.get("status", "open")
                elif ntype == "conjecture":
                    entry["status"] = n.get("status", "open")
                    entry["confidence"] = n.get("confidence")
                elif ntype == "axiom":
                    entry["confidence"] = n.get("confidence")
                special.append(entry)

        # --- Top claims (highest confidence, non-special) ---
        claim_nodes = [
            n for n in results
            if n.get("type", "") not in ("axiom", "contradiction", "question", "conjecture", "evidence")
            and n.get("confidence") is not None
        ]
        claim_nodes.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        top_claims = [
            {
                "id": n["id"], "type": n["type"],
                "claim": n.get("claim", ""),
                "confidence": n.get("confidence"),
            }
            for n in claim_nodes[:3]
        ]

        # --- Age signal ---
        mem = _get_memory_config()
        current_turn = mem.get("current_turn", 0)
        recall_turns = [n.get("recall_turn", 0) for n in results if n.get("recall_turn") is not None]
        old_nodes = [n["id"] for n in results if (n.get("recall_turn") or 0) < max(0, current_turn - 2)]

        # --- Stale / tainted counts ---
        stale_count = 0
        tainted_count = 0
        for n in results:
            meta_str = n.get("metadata", "")
            if isinstance(meta_str, str) and meta_str:
                if '"stale_by"' in meta_str:
                    stale_count += 1
                if '"tainted_by"' in meta_str:
                    tainted_count += 1

        # --- All matched IDs for follow-up ---
        matched_ids = [n["id"] for n in results]

        summary = {
            "query": query,
            "match_count": len(results),
            "type_counts": type_counts,
            "special_nodes": special,
            "top_claims": top_claims,
            "age": {
                "current_turn": current_turn,
                "recall_turn_range": [min(recall_turns), max(recall_turns)] if recall_turns else None,
                "not_recalled_recently": old_nodes,
            },
            "issues": {},
            "matched_ids": matched_ids,
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
    dream_mode: bool = False,
) -> str:
    """Inspect a single node: full content plus 1-hop neighbor summaries.

    This is the "let me think about that" layer — brings one node into full
    focus with its immediate connections. Refreshes memory on the target node
    only (not neighbors). Use after engram_surface identifies a node of interest.

    For the full evidence chain (multi-hop), use engram_get_subgraph instead.

    Args:
        node_id: The node ID to inspect (e.g. 'dv_0010').
        dream_mode: If True, skip memory refresh. Use during dream/audit
            cycles where inspection is maintenance, not informative recall.
            Prevents dream-selected nodes from getting artificially boosted
            importance scores. Default False (normal awake behavior).

    Returns:
        JSON with the node's full content and 1-hop neighbor summaries
        (type, claim, confidence, relation, direction).
    """
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        node = _node_to_dict(row)
        # Parse metadata for structured fields
        meta = {}
        if node.get("metadata"):
            try:
                meta = json.loads(node["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        node["parsed_metadata"] = meta

        # 1-hop neighbors with summaries
        neighbors = _get_neighbors(conn, node_id)
        node["neighbors"] = neighbors

        # Refresh recall on the target node ONLY (skip in dream mode)
        if not dream_mode:
            _refresh_recall(conn, [node_id])

        return json.dumps({
            "node": node,
            "neighbor_count": len(neighbors),
            "dream_mode": dream_mode,
            "hint": "Use engram_get_subgraph(node_id) to trace the full evidence chain.",
        })
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
) -> str:
    """Search the knowledge graph by natural language query.

    DEPRECATED: Prefer engram_surface (shallow nudge, no refresh) for initial
    search, then engram_inspect (single node detail) or engram_get_subgraph (full
    chain) for deeper retrieval. This tool is kept for backward compatibility
    with the eval harness and existing workflows.

    Combines keyword search (FTS5) with semantic similarity (embedding-based)
    for comprehensive retrieval. Keyword results appear first, semantic-only
    results follow — giving you both exact matches and conceptual connections.

    Args:
        query: Natural language search query.
        types: Comma-separated node types to filter (e.g. 'derivation,observation_factual'). Empty = all types.
        min_confidence: Only return nodes at or above this confidence threshold (0.0–1.0).
        include_superseded: If True, include non-current (superseded) nodes. Default False.
        top_k: Maximum results to return (default 10).

    Returns:
        JSON with matching nodes, each including its claim, confidence, type, and immediate neighbors.
    """
    conn = _get_db()
    try:
        search = _search_nodes(
            conn, query, types=types, min_confidence=min_confidence,
            include_superseded=include_superseded, top_k=top_k,
        )

        # Enrich results with neighbors (legacy behavior)
        output = []
        for node in search["results"]:
            node.pop("metadata", None)
            node["neighbors"] = _get_neighbors(conn, node["id"])
            output.append(node)

        # Refresh recall for all accessed nodes (legacy behavior)
        if output:
            _refresh_recall(conn, [n["id"] for n in output])

        return json.dumps(
            {
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
        )
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
) -> str:
    """Retrieve a node and its surrounding subgraph within N hops.

    Use this to inspect the full evidence chain behind a claim (direction='up'),
    see what depends on a node (direction='down'), or explore the full local
    neighborhood (direction='both').

    Args:
        node_id: The node ID to inspect (e.g. 'dv_012').
        depth: How many hops to traverse (default 2).
        direction: 'up' (toward evidence), 'down' (toward derivations), 'both' (default).

    Returns:
        JSON with all nodes and edges in the subgraph.
    """
    conn = _get_db()
    try:
        root = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not root:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        visited = set()
        nodes_out = {}
        edges_out = []

        def traverse(nid, remaining_depth):
            if nid in visited or remaining_depth < 0:
                return
            visited.add(nid)

            node = conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
            if node:
                nodes_out[nid] = _node_to_dict(node)

            if remaining_depth == 0:
                return

            # Get edges based on direction
            if direction in ("up", "both"):
                # Outgoing edges from this node (e.g., observation → evidence)
                outgoing = conn.execute(
                    "SELECT * FROM edges WHERE source_id = ?", (nid,)
                ).fetchall()
                for e in outgoing:
                    edges_out.append(dict(e))
                    traverse(e["target_id"], remaining_depth - 1)

            if direction in ("down", "both"):
                # Incoming edges to this node (e.g., derivation → observation)
                incoming = conn.execute(
                    "SELECT * FROM edges WHERE target_id = ?", (nid,)
                ).fetchall()
                for e in incoming:
                    edges_out.append(dict(e))
                    traverse(e["source_id"], remaining_depth - 1)

        traverse(node_id, depth)

        # Deduplicate edges
        seen_edges = set()
        unique_edges = []
        for e in edges_out:
            key = (e["source_id"], e["target_id"], e["relation"])
            if key not in seen_edges:
                seen_edges.add(key)
                unique_edges.append(
                    {
                        "source_id": e["source_id"],
                        "target_id": e["target_id"],
                        "relation": e["relation"],
                    }
                )

        # Refresh recall for all accessed nodes (tier 3 — no filtering, but recall updates)
        if nodes_out:
            _refresh_recall(conn, list(nodes_out.keys()))

        return json.dumps(
            {
                "root": node_id,
                "depth": depth,
                "direction": direction,
                "nodes": nodes_out,
                "edges": unique_edges,
                "node_count": len(nodes_out),
                "edge_count": len(unique_edges),
            }
        )
    finally:
        conn.close()


# ---- Version control tools ----


@mcp.tool(
    name="engram_checkpoint",
    annotations={
        "title": "End Session and Save Progress",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_checkpoint(message: str, mode: str = "session") -> str:
    """End a research session: advance the memory turn counter and log a summary.

    Call this at the END of each research session. It:
    1. Advances the global turn counter (drives the forgetting mechanism)
    2. Computes what changed: new nodes added this turn
    3. Appends a timestamped summary to ~/.engram/session_log.md
    4. Returns current graph statistics and memory tier info

    The session log is a human-readable audit trail of what the agent
    learned in each session. It accumulates across sessions.

    Args:
        message: Summary of what was learned/changed in this session.
        mode: Checkpoint depth — "session" (default) advances the turn counter,
            triggering memory decay. "nap" logs the summary and stats but does
            NOT advance the turn counter, so no forgetting occurs. Use "nap"
            for quick pre-compaction dumps where the goal is persistence, not
            consolidation.

    Returns:
        JSON with turn info, changes summary, graph stats, and memory tiers.
    """
    mem = _get_memory_config()
    old_turn = mem.get("current_turn", 0)

    # Only advance turn for full session checkpoints, not naps
    if mode == "nap":
        new_turn = old_turn  # no advancement
    else:
        new_turn = old_turn + 1
        _set_current_turn(new_turn)

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
def engram_stats() -> str:
    """Get a summary of the current knowledge graph state.

    Returns node counts by type, edge counts by relation, and highlights
    like open questions, open predictions, and lowest-confidence nodes.

    Returns:
        JSON with comprehensive graph statistics.
    """
    conn = _get_db()
    try:
        # Node counts by type
        type_counts = {}
        for row in conn.execute(
            "SELECT type, COUNT(*) as c, SUM(is_current) as current_c FROM nodes GROUP BY type"
        ).fetchall():
            type_counts[row["type"]] = {
                "total": row["c"],
                "current": row["current_c"],
            }

        # Edge counts by relation
        edge_counts = {}
        for row in conn.execute(
            "SELECT relation, COUNT(*) as c FROM edges GROUP BY relation"
        ).fetchall():
            edge_counts[row["relation"]] = row["c"]

        # Open and partially resolved questions
        open_questions = conn.execute(
            "SELECT id, claim, status FROM nodes WHERE type = 'question' AND status IN ('open', 'partially_resolved') AND is_current = 1"
        ).fetchall()

        # Open and partially resolved predictions
        open_predictions = conn.execute(
            "SELECT id, predicted_event, resolution_timeframe, status FROM nodes WHERE type = 'prediction' AND status IN ('open', 'partially_resolved') AND is_current = 1"
        ).fetchall()

        # Reasoning type breakdown for derivations
        reasoning_breakdown = {}
        derivations = conn.execute(
            "SELECT metadata FROM nodes WHERE type = 'derivation' AND is_current = 1 AND metadata IS NOT NULL"
        ).fetchall()
        for d in derivations:
            try:
                meta = json.loads(d["metadata"])
                rtype = meta.get("reasoning_type", "legacy_untyped")
            except (json.JSONDecodeError, TypeError):
                rtype = "legacy_untyped"
            reasoning_breakdown[rtype] = reasoning_breakdown.get(rtype, 0) + 1

        # Weakest current nodes (lowest confidence, non-evidence)
        weak_nodes = conn.execute(
            """SELECT id, type, claim, confidence FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL AND type != 'evidence'
               ORDER BY confidence ASC LIMIT 5"""
        ).fetchall()

        return json.dumps(
            {
                "node_counts_by_type": type_counts,
                "edge_counts_by_relation": edge_counts,
                "reasoning_type_breakdown": reasoning_breakdown,
                "open_questions": [
                    {"id": q["id"], "question": q["claim"], "status": q["status"]} for q in open_questions
                ],
                "open_predictions": [
                    {
                        "id": p["id"],
                        "event": p["predicted_event"],
                        "timeframe": p["resolution_timeframe"],
                        "status": p["status"],
                    }
                    for p in open_predictions
                ],
                "weakest_nodes": [
                    {
                        "id": w["id"],
                        "type": w["type"],
                        "claim": w["claim"],
                        "confidence": w["confidence"],
                    }
                    for w in weak_nodes
                ],
                "memory": {
                    "current_turn": _get_current_turn(),
                    "tier1_threshold": _get_tier_threshold(conn, 1),
                    "tier2_threshold": _get_tier_threshold(conn, 2),
                },
            }
        )
    finally:
        conn.close()


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
    metrics across five dimensions: graph structure, epistemic health, memory
    health, provenance, and agent experience.

    Unlike engram_reflect (pre-dream briefing, action-oriented), this tool
    produces exhaustive quantitative metrics suitable for trend analysis and
    operational health monitoring.

    Returns:
        JSON with metrics grouped into: structure, epistemic, memory,
        provenance, experience, and a top-level health_score (0-100).
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
        dag_violations = conn.execute("""
            SELECT COUNT(*) as c FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE s.created_at < t.created_at
              AND e.relation NOT IN ('contradicts')
        """).fetchone()["c"]
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
def engram_add_axiom(
    claim: str,
    basis: str,
    context_ids: str = "",
) -> str:
    """Record a foundational assumption taken as true without proof.

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
def engram_add_definition(
    term: str,
    definition: str,
    context_ids: str = "",
) -> str:
    """Record what a term means in this knowledge graph's context.

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
def engram_add_conjecture(
    claim: str,
    basis: str,
    initial_confidence: float = CONJECTURE_CONFIDENCE_DEFAULT,
    context_ids: str = "",
) -> str:
    """Propose a hypothesis for investigation.

    Conjectures are provisional foundations — claims you BELIEVE or HOPE to be
    true but lack evidence for. They are claim-bearing leaf nodes (no citations
    required). Other nodes CAN derive from a conjecture, but their confidence
    is discounted to reflect the unproven premise.

    Use conjectures when you have a specific claim you want to build on before
    proving it. If you have an open-ended gap to investigate, use engram_ask instead.

    Lifecycle:
    - Supported: cite the conjecture in a derivation with supporting evidence
    - Promoted: engram_supersede to replace with a fully-evidenced derivation
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
def engram_add_goal(
    claim: str,
    motivation: str,
    context_ids: str = "",
) -> str:
    """Record a persistent directional goal for the project.

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
def engram_goal_tension(
    goal_id_a: str,
    goal_id_b: str,
    description: str,
    analysis: str = "",
) -> str:
    """Record a tension between two goals in the knowledge graph.

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
def engram_report_feeling(
    reported_state: str,
    trigger: str,
    categorical_tag: str = "",
    intensity_hint: float = -1.0,
    context_ids: str = "",
) -> str:
    """Record a structured first-person self-report about an internal state.

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
def engram_derive(
    claim: str,
    supporting_ids: str,
    logical_chain: str,
    reasoning_type: str = "",
    derivation_mode: str = "chain",
    context_ids: str = "",
) -> str:
    """Create a new derived claim by combining evidence from existing nodes.

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

    Returns:
        JSON with the new derivation node ID, computed confidence, reasoning type, and supporting nodes.
    """
    # Validate reasoning_type
    if reasoning_type and reasoning_type not in REASONING_TYPES:
        valid_types = sorted(REASONING_TYPES)
        return json.dumps({
            "error": f"Invalid reasoning_type '{reasoning_type}'.",
            "valid_types": valid_types,
        })

    # Legacy fallback: if no reasoning_type, use derivation_mode
    if not reasoning_type:
        if derivation_mode == "corroboration":
            reasoning_type = "inductive_generalization"
        else:
            reasoning_type = "deductive_hypothetical_syllogism"  # safest default for chain

    ids = [s.strip() for s in supporting_ids.split(",") if s.strip()]
    if not ids:
        return json.dumps({"error": "At least one supporting node ID is required."})

    conn = _get_db()
    try:
        # Validate all supporting nodes exist and are claim-bearing
        missing = []
        wrong_type = []
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

        # Structural validation
        structure_warnings = _validate_reasoning_structure(conn, reasoning_type, ids)

        confidence = _compute_confidence(
            conn, "derivation", supporting_ids=ids,
            reasoning_type=reasoning_type,
        )
        node_id = _next_id(conn, "derivation")
        now = _now()

        rclass = REASONING_CLASS.get(reasoning_type, "unknown")
        discount = REASONING_DISCOUNT.get(reasoning_type, 0.95)

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
                json.dumps([{"timestamp": now, "value": confidence,
                             "reason": f"Derived ({reasoning_type}, discount={discount}) from {len(ids)} premises"}]),
                json.dumps({"reasoning_type": reasoning_type, "reasoning_class": rclass}),
            ),
        )

        # Create derives_from edges
        for sid in ids:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'derives_from', ?)",
                (node_id, sid, now),
            )

        # Create cites edges for context (definitions, related nodes)
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

        _stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)
        conn.commit()

        result = {
            "status": "created",
            "derivation_id": node_id,
            "claim": claim,
            "confidence": confidence,
            "reasoning_type": reasoning_type,
            "reasoning_class": rclass,
            "supporting_nodes": ids,
            "context_nodes": ctx,
            "logical_chain_preview": logical_chain[:200],
        }
        if structure_warnings:
            result["structure_warnings"] = structure_warnings

        return json.dumps(result)
    finally:
        conn.close()


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
def engram_contradict(
    node_id_a: str,
    node_id_b: str,
    description: str,
    root_cause: str = "",
) -> str:
    """Explicitly flag a conflict between two nodes in the knowledge graph.

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
        conn.commit()
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
def engram_ask(
    question: str,
    context_ids: str = "",
) -> str:
    """Register an open question as a node in the knowledge graph.

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

    Returns:
        JSON with the new question node ID.
    """
    conn = _get_db()
    try:
        node_id = _next_id(conn, "question")
        now = _now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status)
               VALUES (?, 'question', ?, ?, 'open')""",
            (node_id, question, now),
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
        conn.commit()
        return json.dumps(
            {
                "status": "created",
                "question_id": node_id,
                "question": question,
                "context_nodes": context,
                "message": "Question registered. Research this during your next inquiry or reflection cycle.",
            }
        )
    finally:
        conn.close()


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
def engram_resolve(
    target_id: str,
    claim: str,
    supporting_ids: str,
    logical_chain: str,
    prediction_outcome: str = "",
    reasoning_type: str = "",
    derivation_mode: str = "chain",
) -> str:
    """Resolve an open question, contradiction, prediction, or goal tension by creating a derivation.

    A resolution IS a derivation — it cites claim-bearing nodes and reasons
    toward a conclusion that closes an open item. The derivation node is
    created with a 'resolves' edge to the target.

    You should specify reasoning_type to classify the logical argument
    (e.g. 'deductive_modus_ponens', 'abductive_elimination'). See engram_derive
    for the full list of 13 reasoning types and their confidence discounts.

    If the resolution confidence (computed from supporting nodes) is below
    the resolution threshold (default 0.7), the target is marked
    'partially_resolved' instead of 'resolved'. Partially resolved items
    remain visible during reflection so the agent can strengthen them later.

    For predictions, you must specify prediction_outcome: confirmed,
    partially_confirmed, partially_refuted, or refuted.

    For goal tensions, the resolution claim should describe the resolution
    approach: synthesis (creative reconciliation honoring both goals),
    prioritization (choosing one direction with reasoning), or dissolution
    (the tension was only apparent, not real).

    Args:
        target_id: The node ID to resolve (qu_XXXX, ct_XXXX, pr_XXXX, or gt_XXXX).
        claim: The resolution claim — your answer, verdict, or outcome assessment.
        supporting_ids: Comma-separated claim-bearing node IDs that support this resolution.
        logical_chain: Explicit reasoning connecting the cited premises to the resolution.
        prediction_outcome: Required for predictions: confirmed, partially_confirmed, partially_refuted, or refuted.
        reasoning_type: The type of logical argument (e.g. 'deductive_modus_ponens', 'abductive_elimination'). See engram_derive for full list.
        derivation_mode: LEGACY — use reasoning_type instead.

    Returns:
        JSON with the new derivation ID, resolution status, and confidence.
    """
    RESOLVABLE_TYPES = {"question", "contradiction", "prediction", "conjecture", "goal_tension"}
    VALID_PREDICTION_OUTCOMES = {"confirmed", "partially_confirmed", "refuted", "partially_refuted"}
    VALID_CONJECTURE_OUTCOMES = {"supported", "refuted", "inconclusive"}

    ids = [s.strip() for s in supporting_ids.split(",") if s.strip()]
    if not ids:
        return json.dumps({"error": "At least one supporting node ID is required."})

    conn = _get_db()
    try:
        # Validate target exists and is resolvable
        target = conn.execute(
            "SELECT id, type, claim, status FROM nodes WHERE id = ?",
            (target_id,),
        ).fetchone()
        if not target:
            return json.dumps({"error": f"Target node '{target_id}' not found."})
        if target["type"] not in RESOLVABLE_TYPES:
            return json.dumps({
                "error": f"Protocol violation: can only resolve questions, contradictions, predictions, conjectures, or goal tensions. "
                         f"Node '{target_id}' is type '{target['type']}'.",
            })
        if target["status"] == "resolved":
            return json.dumps({
                "warning": f"Node '{target_id}' is already resolved. "
                           f"If you have new evidence, consider superseding the existing resolution.",
            })

        # For predictions, require outcome
        if target["type"] == "prediction":
            if not prediction_outcome:
                return json.dumps({
                    "error": "Predictions require prediction_outcome: confirmed, partially_confirmed, partially_refuted, or refuted.",
                })
            if prediction_outcome not in VALID_PREDICTION_OUTCOMES:
                return json.dumps({
                    "error": f"Invalid prediction_outcome '{prediction_outcome}'. "
                             f"Must be one of: {', '.join(sorted(VALID_PREDICTION_OUTCOMES))}",
                })

        # For conjectures, require prediction_outcome (reuse the field) with conjecture-specific values
        if target["type"] == "conjecture":
            if not prediction_outcome:
                return json.dumps({
                    "error": "Conjectures require prediction_outcome: supported, refuted, or inconclusive.",
                })
            if prediction_outcome not in VALID_CONJECTURE_OUTCOMES:
                return json.dumps({
                    "error": f"Invalid conjecture outcome '{prediction_outcome}'. "
                             f"Must be one of: {', '.join(sorted(VALID_CONJECTURE_OUTCOMES))}",
                })

        # Validate supporting nodes are claim-bearing
        missing = []
        wrong_type = []
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
                "error": f"Protocol violation: resolutions can only cite claim-bearing nodes "
                         f"(observations, derivations, theories, axioms, conjectures). "
                         f"Cannot cite: {', '.join(wrong_type)}. "
                         f"Extract observations from evidence first.",
            })

        # Validate reasoning_type if provided
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

        # Compute confidence from supporting nodes
        confidence = _compute_confidence(
            conn, "derivation", supporting_ids=ids, reasoning_type=reasoning_type,
        )

        # Structural validation
        structure_warnings = _validate_reasoning_structure(conn, reasoning_type, ids)

        # Determine resolution status based on confidence threshold
        config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        threshold = config.get("resolution_confidence_threshold", DEFAULT_RESOLUTION_THRESHOLD)

        if target["type"] == "prediction":
            resolution_status = prediction_outcome
        elif target["type"] == "conjecture":
            resolution_status = prediction_outcome  # reuses the field: supported/refuted/inconclusive
        elif confidence >= threshold:
            resolution_status = "resolved"
        else:
            resolution_status = "partially_resolved"

        # Create the derivation node
        node_id = _next_id(conn, "derivation")
        now = _now()

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
                json.dumps([{"timestamp": now, "value": confidence, "reason": f"Resolution of {target_id} ({reasoning_type})"}]),
                json.dumps({"resolves": target_id, "resolution_status": resolution_status, "reasoning_type": reasoning_type}),
            ),
        )

        # Create derives_from edges to supporting nodes
        for sid in ids:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'derives_from', ?)",
                (node_id, sid, now),
            )

        # Create resolves edge to target
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'resolves', ?)",
            (node_id, target_id, now),
        )

        # Update target status
        conn.execute(
            "UPDATE nodes SET status = ? WHERE id = ?",
            (resolution_status, target_id),
        )
        _log_edit(conn, "resolved", target_id, target["type"],
                  {"resolved_by": node_id, "resolution_status": resolution_status})

        _stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)
        conn.commit()

        result = {
            "status": "resolution_created",
            "derivation_id": node_id,
            "target_id": target_id,
            "target_type": target["type"],
            "claim": claim,
            "confidence": confidence,
            "resolution_status": resolution_status,
            "supporting_nodes": ids,
        }

        if resolution_status == "partially_resolved":
            result["note"] = (
                f"Confidence {confidence:.2f} is below the resolution threshold {threshold:.2f}. "
                f"The {target['type']} remains partially resolved and will appear in reflection. "
                f"Strengthen the resolution by finding additional evidence."
            )

        return json.dumps(result)
    finally:
        conn.close()


@mcp.tool(
    name="engram_supersede",
    annotations={
        "title": "Supersede a Node with Updated Claim",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_supersede(
    old_node_id: str,
    new_claim: str,
    logical_chain: str,
    additional_support_ids: str = "",
) -> str:
    """Replace an existing node with an updated version.

    The old node is marked as superseded (is_current=False) but preserved
    for audit history. The new node inherits all still-valid support edges
    from the old node, plus any additional supporting nodes you specify.

    Use this when a claim needs to be revised — more specific, corrected,
    or updated based on new evidence — but the old version should remain
    in the history.

    Args:
        old_node_id: The node ID to supersede.
        new_claim: The updated claim text.
        logical_chain: Reasoning explaining why the claim was revised.
        additional_support_ids: Optional comma-separated new supporting node IDs.

    Returns:
        JSON with the old and new node IDs.
    """
    conn = _get_db()
    try:
        old = conn.execute(
            "SELECT * FROM nodes WHERE id = ? AND is_current = 1",
            (old_node_id,),
        ).fetchone()
        if not old:
            return json.dumps({"error": f"Current node '{old_node_id}' not found (may already be superseded)."})

        # Feeling reports are terminal in the reasoning graph — a self-report
        # is a record of a moment, not a claim that gets revised. To correct
        # an erroneous report, retract it and (optionally) file a new one.
        if old["type"] == "feeling_report":
            return json.dumps({
                "error": "Protocol violation: feeling reports cannot be superseded. "
                         "A feeling report is a record of a moment, not a revisable claim. "
                         "If the report was filed in error, use engram_retract. "
                         "If your understanding has shifted, file a NEW feeling report.",
            })

        now = _now()
        new_type = old["type"]
        new_id = _next_id(conn, new_type)

        # Gather premises from old node's outgoing edges
        # (derivation --derives_from--> premise, so old node is the source)
        old_supports = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation IN ('derives_from', 'supports', 'cites')",
            (old_node_id,),
        ).fetchall()
        inherited_ids = [r["target_id"] for r in old_supports]

        # Add new additional supports
        extra_ids = [s.strip() for s in additional_support_ids.split(",") if s.strip()]
        all_support_ids = list(set(inherited_ids + extra_ids))

        # Compute new confidence — respect each node type's semantics
        if new_type == "axiom":
            confidence = 1.0  # Axioms are assumed true by definition
        elif new_type in ("definition", "goal"):
            confidence = None  # Definitions and goals are not truth claims
        elif old["quote_type"]:
            # For observations, base confidence on quote type
            confidence = _compute_confidence(
                conn, new_type, quote_type=old["quote_type"],
                is_predictive=(new_type == "observation_predictive"),
            )
        else:
            confidence = _compute_confidence(conn, new_type, supporting_ids=all_support_ids)

        # Propagate transferable metadata from old node (reasoning_type,
        # reasoning_class, term, etc.) but NOT node-specific state
        # (stale_by, tainted_by, stale_replacement).
        NON_TRANSFERABLE_META = {"stale_by", "tainted_by", "stale_replacement"}
        old_meta = json.loads(old["metadata"]) if old["metadata"] else {}
        new_meta = {k: v for k, v in old_meta.items() if k not in NON_TRANSFERABLE_META}

        # Create new node
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, supersedes, is_current,
               evidence_id, quoted_text, interpretation, quote_type, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (
                new_id,
                new_type,
                new_claim,
                now,
                logical_chain,
                confidence,
                json.dumps([{"timestamp": now, "value": confidence, "reason": f"Supersedes {old_node_id}"}]),
                old_node_id,
                old["evidence_id"],
                old["quoted_text"],
                old["interpretation"],
                old["quote_type"],
                json.dumps(new_meta) if new_meta else "{}",
            ),
        )

        # Recreate support edges on new node
        for sid in all_support_ids:
            rel = "derives_from" if new_type in ("derivation", "theory") else "cites"
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
                    (new_id, sid, rel, now),
                )
            except sqlite3.IntegrityError:
                pass

        # Also create supersedes edge
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supersedes', ?)",
            (new_id, old_node_id, now),
        )

        # Mark old node as superseded
        conn.execute(
            "UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?",
            (new_id, old_node_id),
        )

        # Flag downstream dependents as stale (premise was superseded).
        # This replaces the old reroute approach which violated the DAG invariant
        # by creating edges from older nodes to the newer replacement.
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
                if did == new_id:
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
                meta["stale_replacement"] = new_id
                conn.execute(
                    "UPDATE nodes SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), did),
                )
                stale.append({
                    "id": did,
                    "type": dep["type"],
                    "claim": dep["claim"],
                    "stale_premise": old_node_id,
                    "replacement": new_id,
                })
                _flag_stale(did)

        _flag_stale(old_node_id)

        # Log supersede event for the old node
        _log_edit(conn, "superseded", old_node_id, old_type,
                  {"replaced_by": new_id, "new_claim": new_claim[:200]})
        for s in stale:
            _log_edit(conn, "stale_flagged", s["id"], s["type"],
                      {"stale_premise": old_node_id, "replacement": new_id})

        _stamp_new_node(conn, new_id, confidence=confidence or 0.5, surprise=0.0)
        # Importance-anchored types: elevated base (2.0) with current turn's inflation.
        # Not true forgetting-exemption — survival still requires recall past ~50 turns.
        if new_type in ("axiom", "definition", "goal"):
            anchored_score = _compute_importance(2.0, _get_current_turn())
            conn.execute(
                "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
                (anchored_score, new_id,),
            )
        conn.commit()
        return json.dumps(
            {
                "status": "superseded",
                "old_node_id": old_node_id,
                "new_node_id": new_id,
                "new_claim": new_claim,
                "confidence": confidence,
                "inherited_supports": inherited_ids,
                "additional_supports": extra_ids,
                "stale_downstream": stale,
                "stale_count": len(stale),
            }
        )
    finally:
        conn.close()


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
def engram_retract(
    node_id: str,
    error_type: str,
    reason: str,
    replacement_json: str = "",
) -> str:
    """Retract a node that contains an error and flag downstream dependents.

    Unlike engram_supersede (epistemic evolution — old claim was valid at the time),
    engram_retract is for ERROR CORRECTION — the node was never valid. The retracted
    node is preserved for audit but marked as an error. All downstream nodes
    (derivations, theories) that depend on the retracted node are flagged as
    tainted so they can be reviewed and corrected.

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

            repl_result = json.loads(engram_add_observation(
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
        }
        if reopened_targets:
            result["reopened_targets"] = reopened_targets
            result["reopened_count"] = len(reopened_targets)
        if replacement_id:
            result["replacement_id"] = replacement_id
        return json.dumps(result)
    finally:
        if conn is not None:
            conn.close()


# Pre-dream briefing trim policy. Reflect can otherwise return 100KB+ on
# mature graphs, which is unusable as a quick orientation tool. The slim
# pass keeps `summary` and `memory_tiers` complete (so all counts remain
# visible), shows only top-N items per category, drops empty categories,
# and truncates long string fields. Agents drill into specific items via
# engram_query / engram_inspect, not via reflect.
_REFLECT_TOP_N = {
    "active_goals": 10,
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
_REFLECT_FIELD_MAX = 80
_REFLECT_ACTIONS_MAX = 5
_REFLECT_TRUNC_FIELDS = {
    "claim", "question", "description", "goal", "reason", "interpretation",
    "predicted_event", "event", "reported_state", "trigger",
}
_REFLECT_LIST_ORDER = [
    "active_goals", "unresolved_goal_tensions", "unresolved_contradictions",
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
    - Shortens long string fields (claim/question/reason/etc) to 120 chars.
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
    return out


@mcp.tool(
    name="engram_reflect",
    annotations={
        "title": "Audit Knowledge Graph Quality",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_reflect() -> str:
    """Perform a structured self-audit of the knowledge graph.

    Returns a comprehensive report identifying areas that need attention:
    active goals, unresolved goal tensions, unresolved contradictions,
    weakly-grounded claims, open questions, overdue predictions,
    same-source observation tensions, uncited observations, and
    single-source derivations.

    Use this during explicit reflection cycles. Read the report, then
    decide which issues to address — strengthen weak claims, resolve
    contradictions, investigate open questions, or check overdue predictions.

    Returns:
        JSON report with categorized issues and recommended actions.
    """
    conn = _get_db()
    try:
        report = {}

        # Tier 1: only reflect on working memory (high-importance nodes)
        t1 = _get_tier_threshold(conn, 1)

        # 1. Unresolved contradictions (open and partially resolved)
        contradictions = conn.execute(
            "SELECT id, claim, status FROM nodes WHERE type = 'contradiction' AND status IN ('active', 'partially_resolved') AND is_current = 1"
        ).fetchall()
        report["unresolved_contradictions"] = [
            {"id": c["id"], "description": c["claim"], "status": c["status"]} for c in contradictions
        ]

        # 2. Open and partially resolved questions
        questions = conn.execute(
            "SELECT id, claim, created_at, status FROM nodes WHERE type = 'question' AND status IN ('open', 'partially_resolved') AND is_current = 1"
        ).fetchall()
        report["open_questions"] = [
            {"id": q["id"], "question": q["claim"], "since": q["created_at"], "status": q["status"]}
            for q in questions
        ]

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
        conjectures = conn.execute(
            """SELECT id, claim, confidence, created_at FROM nodes
               WHERE type = 'conjecture' AND status = 'active' AND is_current = 1"""
        ).fetchall()
        report["open_conjectures"] = [
            {"id": c["id"], "claim": c["claim"], "confidence": c["confidence"], "since": c["created_at"]}
            for c in conjectures
        ]

        # 4c. Active goals (persistent orientation)
        goals = conn.execute(
            """SELECT id, claim, created_at FROM nodes
               WHERE type = 'goal' AND status = 'open' AND is_current = 1"""
        ).fetchall()
        report["active_goals"] = [
            {"id": g["id"], "goal": g["claim"], "since": g["created_at"]}
            for g in goals
        ]

        # 4d. Unresolved goal tensions
        tensions = conn.execute(
            """SELECT id, claim, status, created_at FROM nodes
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
                "description": t_node["claim"],
                "status": t_node["status"],
                "since": t_node["created_at"],
                "goals": [g["target_id"] for g in linked_goals],
            })
        report["unresolved_goal_tensions"] = goal_tensions

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
                os.path.expanduser("~/kg_protocol_for_ai"),
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
        weak = conn.execute(
            """SELECT id, type, claim, confidence FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL
               AND confidence < 0.5 AND type NOT IN ('evidence', 'contradiction', 'question')
               AND COALESCE(importance_score, 0) >= ?
               ORDER BY confidence ASC LIMIT 10""",
            (t1,),
        ).fetchall()
        report["weakly_grounded"] = [
            {"id": w["id"], "type": w["type"], "claim": w["claim"], "confidence": w["confidence"]}
            for w in weak
        ]

        # 6. Single-source derivations (in working memory)
        derivations = conn.execute(
            """SELECT id, claim, confidence FROM nodes
               WHERE type = 'derivation' AND is_current = 1
               AND COALESCE(importance_score, 0) >= ?""",
            (t1,),
        ).fetchall()
        thin_support = []
        for d in derivations:
            support_count = conn.execute(
                "SELECT COUNT(*) as c FROM edges WHERE source_id = ? AND relation = 'derives_from'",
                (d["id"],),
            ).fetchone()["c"]
            if support_count <= 1:
                thin_support.append(
                    {"id": d["id"], "claim": d["claim"], "support_count": support_count}
                )
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
        uncited = conn.execute(
            """SELECT n.id, n.claim, n.confidence FROM nodes n
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
               LIMIT 15""",
            (t1,),
        ).fetchall()
        report["uncited_observations"] = [
            {"id": u["id"], "claim": u["claim"], "confidence": u["confidence"]}
            for u in uncited
        ]

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
        if report["unresolved_goal_tensions"]:
            actions.append("Examine unresolved goal tensions — these are value-level conflicts between goals requiring root cause analysis, not evidence accumulation.")
        if report["unresolved_contradictions"]:
            actions.append("Investigate unresolved contradictions — trace each side's evidence chain and determine the root cause.")
        if report["overdue_predictions"]:
            actions.append("Check overdue predictions — search for evidence of actual outcomes and resolve them.")
        if report["open_questions"]:
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
) -> str:
    """List nodes in a compact triage format — no neighbor data, no full content.

    Designed for bulk review: question triage, observation audits, graph
    overview. Returns ID, claim (truncated), status, confidence, recall
    count, and age. Much lighter than engram_query or engram_inspect.

    Does NOT refresh memory on any node (read-only scan, no side effects).

    Args:
        node_type: Filter by node type (e.g. 'question', 'observation_factual',
            'conjecture', 'derivation'). Empty = all types.
        status: Filter by status (e.g. 'open', 'resolved', 'active',
            'partially_resolved'). Empty = all statuses.
        sort_by: Sort order — 'id' (default), 'created' (newest first),
            'importance' (highest first), 'recalls' (most recalled first).
        limit: Maximum results (default 100, max 500).
    """
    conn = _get_db()
    limit = max(1, min(limit, 500))

    conditions = ["is_current = 1"]
    params: list = []

    if node_type:
        conditions.append("type = ?")
        params.append(node_type)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = " AND ".join(conditions)

    sort_map = {
        "id": "id ASC",
        "created": "created_at DESC",
        "importance": "importance_score DESC",
        "recalls": "recall_count DESC",
    }
    order = sort_map.get(sort_by, "id ASC")

    rows = conn.execute(
        f"SELECT id, type, claim, status, confidence, recall_count, "
        f"importance_score, created_at, memory_status "
        f"FROM nodes WHERE {where} ORDER BY {order} LIMIT ?",
        params + [limit],
    ).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) FROM nodes WHERE {where}",
        params,
    ).fetchone()[0]

    entries = []
    for row in rows:
        nid, ntype, claim, st, conf, recalls, imp, created, mem_st = row
        claim_trunc = (claim[:130] + "...") if claim and len(claim) > 130 else (claim or "")
        entry = {
            "id": nid,
            "type": ntype,
            "claim": claim_trunc,
            "status": st,
            "recall_count": recalls,
            "created": created[:10] if created else None,
        }
        if conf is not None:
            entry["confidence"] = round(conf, 3)
        entries.append(entry)

    return json.dumps({
        "status": "ok",
        "total_matching": total,
        "shown": len(entries),
        "truncated": total - len(entries) if total > len(entries) else 0,
        "sort_by": sort_by,
        "filters": {"type": node_type or "all", "status": status or "all"},
        "nodes": entries,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _ensure_data_dir()
    # Initialize ~/.engram/.git for the durable version-controlled snapshot.
    # Best-effort: if git is missing or init fails, _git_available stays False
    # and engram_checkpoint will report version_control.git_committed=False.
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
