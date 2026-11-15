"""engram_embedding_publish — the shared strict embedding-publication primitive (#4651 part A).

ONE function, two entry modes, one verification:

- compute mode (``prepared=None``): the strict summary-write refresh as merged
  in #4739 — encode the node's current recipe, store it, verify both vector
  stores, restore both and record debt on any failure.
- prepared mode: the vector arrives already encoded. It never calls the
  embedder, its availability probe, or ``core._compute_and_store_embedding``;
  encoding is the ONLY thing it skips. It first runs six ordered checks (the
  first failure defers and writes nothing), then publishes through the same
  store step, the same verification, the same rollback and the same single
  debt-clearer as compute mode.

The caller owns the transaction: this module never commits and never opens a
transaction of its own. It uses a SAVEPOINT, so it nests inside the caller's.

HOUSE RULES (mirror engram_core.py § HOUSE RULES):
- Access shared state ONLY via ``import engram_core as core; core.NAME`` — never
  via ``from engram_core import NAME``.
- This module must not import server.py (acyclic: server → family → core).
- No module-level mutable assignments — all state lives in engram_core.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import engram_core as core

EMBED_DIMENSION = 384
EMBED_NORMALISATION = "model-default"
# The embedder calls model.encode(text, convert_to_numpy=True) with NO
# normalize_embeddings argument (engram_core, EmbeddingManager.embed). The token
# names that call. If that call changes, this token MUST change with it.

_EMBEDDING_REFRESH_PENDING = "embedding_refresh_pending"
_SAVEPOINT = "recall_summary_vector_refresh"
_MARKER_OPERATION = "engram_set_recall_summaries"


@dataclass(frozen=True)
class EmbeddingEpoch:
    model: str
    dimension: int
    normalisation: str


@dataclass(frozen=True)
class PreparedVector:
    vector: list
    text_sha256: str
    epoch: EmbeddingEpoch


@dataclass(frozen=True)
class PublishResult:
    node_id: str
    status: str  # "refreshed" | "pending" | "deferred"
    reason: Optional[str]
    vector_changed: Optional[bool]
    debt_cleared: bool
    warning: Optional[str]


def current_epoch() -> EmbeddingEpoch:
    model = core._get_embedding_config().get("model", core.DEFAULT_EMBEDDING_MODEL)
    return EmbeddingEpoch(model=model, dimension=EMBED_DIMENSION,
                          normalisation=EMBED_NORMALISATION)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_recipe(conn, node_id: str) -> tuple:
    """(text, sha256 of that text): what part B encodes and hashes at encode time."""
    text = core._embedding_text_for_node(conn, node_id)
    return text, text_sha256(text)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _vec_store_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vec_nodes'"
    ).fetchone() is not None


def _set_embedding_refresh_marker(conn, node_id: str, metadata_raw, *, reason: str) -> Optional[str]:
    """Persist strict-refresh debt while preserving unrelated metadata keys."""
    metadata, warning = core._metadata_dict_for_write(
        metadata_raw, node_id, _MARKER_OPERATION
    )
    metadata[_EMBEDDING_REFRESH_PENDING] = {
        "reason": reason, "stage": "strict_refresh", "at": _utc_now(),
    }
    if conn.execute(
        "UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(metadata), node_id)
    ).rowcount != 1:
        raise RuntimeError("could not persist embedding refresh debt marker")
    return warning


def _clear_embedding_refresh_marker(conn, node_id: str, metadata_raw) -> Optional[str]:
    """The sole debt-clearer: a verified strict refresh of this one node."""
    if _EMBEDDING_REFRESH_PENDING not in core._metadata_dict(metadata_raw):
        return None
    metadata, warning = core._metadata_dict_for_write(
        metadata_raw, node_id, _MARKER_OPERATION
    )
    metadata.pop(_EMBEDDING_REFRESH_PENDING, None)
    if conn.execute(
        "UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(metadata), node_id)
    ).rowcount != 1:
        raise RuntimeError("could not clear embedding refresh debt marker")
    return warning


def _pending_reason(outcome) -> str:
    if outcome.status == "skipped":
        return "unavailable"
    if outcome.stage in {"text", "embed", "store"}:
        return outcome.stage
    return "embedding"


def _deferred(node_id: str, reason: str) -> PublishResult:
    return PublishResult(node_id=node_id, status="deferred", reason=reason,
                         vector_changed=None, debt_cleared=False, warning=None)


def _valid_vector(vector) -> bool:
    if not isinstance(vector, list) or len(vector) != EMBED_DIMENSION:
        return False
    for item in vector:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return False
        if not math.isfinite(item):
            return False
    return True


def _epoch_matches(prepared_epoch) -> bool:
    now = current_epoch()
    missing = object()
    return (
        getattr(prepared_epoch, "model", missing) == now.model
        and getattr(prepared_epoch, "dimension", missing) == now.dimension
        and getattr(prepared_epoch, "normalisation", missing) == now.normalisation
    )


def _metadata_is_object_or_null(raw) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, str):
        return False
    try:
        return isinstance(json.loads(raw), dict)
    except (TypeError, ValueError):
        return False


def _prepared_deferral(conn, node_id: str, prepared: PreparedVector) -> Optional[str]:
    """The six ordered prepared-mode checks; the first failure names the reason."""
    row = conn.execute(
        "SELECT is_current, metadata FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None or row[0] != 1:
        return "not_current"
    text = core._embedding_text_for_node(conn, node_id)
    if not text:
        return "empty_text"
    if text_sha256(text) != prepared.text_sha256:
        return "recipe_changed"
    if not _epoch_matches(prepared.epoch):
        return "epoch_changed"
    if not _valid_vector(prepared.vector):
        return "invalid_vector"
    if not _metadata_is_object_or_null(row[1]):
        return "malformed_metadata"
    return None


def _failure_reason(conn, node_id: str, outcome, expected_vector) -> Optional[str]:
    """Verify a store/compute outcome; None means both vector stores are good."""
    if outcome.status != "embedded":
        return _pending_reason(outcome)
    if not outcome.vec_mirror_ok:
        return "mirror"
    row = conn.execute("SELECT embedding FROM nodes WHERE id = ?", (node_id,)).fetchone()
    stored = row[0] if row else None
    if expected_vector is not None and stored != json.dumps(expected_vector):
        return "canonical"
    try:
        vector = json.loads(stored) if row else None
    except (TypeError, json.JSONDecodeError):
        vector = None
    if not isinstance(vector, list) or not vector:
        return "canonical"
    if _vec_store_exists(conn):
        # Do not accept the core helper's normal-write tolerance or its
        # wrong-dimension mirror bypass in this strict path.
        if (not core._VEC_BACKEND_AVAILABLE or core._sqlite_vec is None
                or len(vector) != EMBED_DIMENSION):
            return "mirror"
        mirror = conn.execute(
            "SELECT embedding FROM vec_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if mirror is None or mirror[0] != core._sqlite_vec.serialize_float32(vector):
            return "mirror"
    return None


def _row_metadata(conn, node_id: str):
    row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row[0] if row else None


def publish_embedding(conn, node_id: str, *,
                      prepared: Optional[PreparedVector] = None) -> PublishResult:
    """Publish one node's embedding, strictly; see the module docstring.

    Does not commit and does not open a transaction: the caller owns it.
    """
    expected_vector = None
    if prepared is not None:
        reason = _prepared_deferral(conn, node_id, prepared)
        if reason is not None:
            return _deferred(node_id, reason)
        expected_vector = prepared.vector

    before = conn.execute("SELECT embedding FROM nodes WHERE id = ?", (node_id,)).fetchone()
    before_embedding = before[0] if before else None

    conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    reason = None
    try:
        if prepared is None:
            outcome = core._compute_and_store_embedding(conn, node_id)
        else:
            outcome = core._store_embedding_vector(conn, node_id, expected_vector)
        reason = _failure_reason(conn, node_id, outcome, expected_vector)
    except Exception:  # savepoint makes unexpected compute/verify failure non-destructive
        reason = "embedding"

    if reason is not None:
        conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
        conn.execute(f"RELEASE {_SAVEPOINT}")
        warning = _set_embedding_refresh_marker(
            conn, node_id, _row_metadata(conn, node_id), reason=reason)
        return PublishResult(node_id=node_id, status="pending", reason=reason,
                             vector_changed=None, debt_cleared=False, warning=warning)

    conn.execute(f"RELEASE {_SAVEPOINT}")
    metadata_raw = _row_metadata(conn, node_id)
    had_marker = _EMBEDDING_REFRESH_PENDING in core._metadata_dict(metadata_raw)
    warning = _clear_embedding_refresh_marker(conn, node_id, metadata_raw)
    after = conn.execute("SELECT embedding FROM nodes WHERE id = ?", (node_id,)).fetchone()
    after_embedding = after[0] if after else None
    return PublishResult(node_id=node_id, status="refreshed", reason=None,
                         vector_changed=after_embedding != before_embedding,
                         debt_cleared=had_marker, warning=warning)
