"""Forum embedding layer — sentence-transformers wrapper + pure-math helpers.

Mirrors the engram server's degradation discipline (server.py:47-60):
loud, not silent. If sentence-transformers or sqlite-vec is missing the
forum degrades gracefully — FTS/LIKE search still works — but the operator
is told about it.

Env gate:
    FORUM_NO_EMBEDDINGS=1  — disables the embedding layer entirely (test mode;
                             mirrors ENGRAM_NO_EMBEDDINGS in server.py:108).

Public API
----------
available() -> bool
    True iff the embedding layer is ready to encode text.

encode(text) -> list[float] | None
    Return a normalized 384-dim vector, or None if unavailable.

encode_batch(texts) -> list[list[float]] | None
    Return a list of normalized 384-dim vectors, or None if unavailable.

Pure-math helpers (no model dependency — importable by tests + backfill with
synthetic vectors):

serialize(vector) -> bytes
    Pack a float list to little-endian float32 bytes (uses
    sqlite_vec.serialize_float32 when available; raw struct pack otherwise).

deserialize(blob) -> list[float]
    Unpack little-endian float32 bytes to a list of floats.
    Works without sqlite-vec (documented layout: N x 4-byte LE float32).

renormalized_mean(vectors) -> list[float]
    Compute the arithmetic mean of a list of vectors then L2-normalize.
    Re-normalization is required for cosine similarity -- an unnormalized
    mean shrinks toward zero on diverse sets, breaking cosine scoring.
    Returns None if vectors is empty.

incremental_centroid(old, n, post_vec) -> list[float]
    Incrementally update a thread centroid when a new post is added:
        new = normalize((old * n + post_vec) / (n + 1))
    where n = number of already-embedded posts in the thread.
    Returns a new normalized centroid.
"""

from __future__ import annotations

import math
import os
import struct
import sys
from typing import Any

# #1762: belt-and-suspenders against HuggingFace Hub's online etag-check
# retry ladder (6 x 10s + backoff ~= 83s stall on a slow/unreachable HF
# Hub -- the same defect fixed for the ENGRAM daemon side in #1682). The
# module singleton loaded by _get_model() below now tries a fully offline
# load first (local_files_only=True), which already avoids any HF Hub
# network call -- this env var is a second line of defense so that even a
# residual online etag-check (e.g. a first-time download) times out in ~1s
# instead of ~83s. Set before the `from sentence_transformers import ...`
# below. setdefault() so an operator override in the environment wins.
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "1")

# ---------------------------------------------------------------------------
# Loud degradation checks -- mirrors server.py:47-62
# ---------------------------------------------------------------------------

try:
    import sqlite_vec as _sqlite_vec  # type: ignore
    _SQLITE_VEC_AVAILABLE = True
except Exception:
    _sqlite_vec = None  # type: ignore
    _SQLITE_VEC_AVAILABLE = False

try:
    from sentence_transformers import SentenceTransformer as _SentenceTransformer  # type: ignore
    _ST_AVAILABLE = True
except ImportError:
    _SentenceTransformer = None  # type: ignore
    _ST_AVAILABLE = False
    if not os.environ.get("FORUM_NO_EMBEDDINGS"):
        print(
            "[forum] sentence-transformers unavailable -- semantic embedding layer is off; "
            "FTS/LIKE search still works. Install sentence-transformers==5.3.0 in this "
            "interpreter to enable post/thread embeddings.",
            file=sys.stderr,
        )

if not _SQLITE_VEC_AVAILABLE:
    if not os.environ.get("FORUM_NO_EMBEDDINGS"):
        print(
            "[forum] sqlite-vec unavailable -- vec0 KNN index for posts/threads is off; "
            "FTS/LIKE search still works. Install sqlite-vec==0.1.9 in this interpreter "
            "to enable the vec0 embedding index (see #728 for context).",
            file=sys.stderr,
        )

# Model name -- same as engram side for consistent semantics + shared weights cache.
# See DEFAULT_EMBEDDING_MODEL in server.py:150.
FORUM_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# ---------------------------------------------------------------------------
# Module-level model singleton
# ---------------------------------------------------------------------------

_model: Any = None  # SentenceTransformer | None


def _get_model() -> Any:
    """Return the loaded model singleton, loading it on first call.

    Model is always-loaded at create_app() time when embeddings are enabled
    (RAM fine per design-settlement -- see issue #807). This function is the
    loading entry point; create_app() calls warm_model() at startup so the
    first post write never bears the model load latency.

    Offline-first (#1762, mirrors #1682 on the ENGRAM daemon side): tries
    local_files_only=True so a cached model never triggers HF Hub's online
    etag-check (source of an ~83s stall when HF Hub is slow/unreachable).
    Falls back to a one-time online download on a genuine cache-miss.
    """
    global _model
    if os.environ.get("FORUM_NO_EMBEDDINGS"):
        return None
    if not _ST_AVAILABLE:
        return None
    if _model is None:
        try:
            try:
                # #1762: offline-first load. A cached model loads with ZERO
                # HuggingFace Hub network traffic -- no online etag-check,
                # so the ~83s stall (HF Hub slow/unreachable) cannot happen
                # on the common warm-cache path (every forum app startup
                # after the first successful load).
                _model = _SentenceTransformer(FORUM_EMBEDDING_MODEL, local_files_only=True)
            except Exception:
                # Cache-miss: this install does NOT pre-download the
                # embedder model as a discrete install step (verified --
                # no forum install/deploy script calls SentenceTransformer(...)).
                # The genuine first-time download happens here, lazily, on
                # first real use. Fall back to a one-time online download
                # so a fresh install keeps working; every subsequent load
                # hits the local_files_only path above.
                print(
                    f"[forum] Embedding model '{FORUM_EMBEDDING_MODEL}' not in "
                    f"local HF cache -- downloading once (online); subsequent "
                    f"loads are offline.",
                    file=sys.stderr,
                )
                _model = _SentenceTransformer(FORUM_EMBEDDING_MODEL)
        except Exception as exc:
            print(
                f"[forum] Failed to load embedding model '{FORUM_EMBEDDING_MODEL}': {exc}",
                file=sys.stderr,
            )
            _model = None
    return _model


def warm_model() -> None:
    """Pre-load the embedding model.

    Called at create_app() time so the model is resident before the first
    request arrives. Safe to call multiple times (idempotent).
    """
    _get_model()


# ---------------------------------------------------------------------------
# Public availability check
# ---------------------------------------------------------------------------

def available() -> bool:
    """Return True iff the embedding layer is ready to encode text.

    False when:
    - FORUM_NO_EMBEDDINGS=1 is set (test/no-embedding mode), OR
    - sentence-transformers is not installed, OR
    - the model failed to load.
    """
    if os.environ.get("FORUM_NO_EMBEDDINGS"):
        return False
    return _get_model() is not None


# ---------------------------------------------------------------------------
# Encode helpers
# ---------------------------------------------------------------------------

def encode(text: str) -> list[float] | None:
    """Return a normalized 384-dim embedding for text, or None if unavailable.

    Normalization: normalize_embeddings=True at encode time so cosine similarity
    is equivalent to dot product -- required for vec0 cosine distance_metric.
    """
    if not text:
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        vec = model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return vec.tolist()
    except Exception as exc:
        print(f"[forum] encode() failed: {exc}", file=sys.stderr)
        return None


def encode_batch(texts: list[str]) -> list[list[float]] | None:
    """Return normalized 384-dim embeddings for a list of texts, or None if unavailable."""
    if not texts:
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        vecs = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return vecs.tolist()
    except Exception as exc:
        print(f"[forum] encode_batch() failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Pure-math helpers (no model dependency)
# These are importable by tests and the backfill script with synthetic vectors.
# ---------------------------------------------------------------------------

def serialize(vector: list[float]) -> bytes:
    """Serialize a float list to binary blob for SQLite storage.

    Uses sqlite_vec.serialize_float32 when available (canonical for vec0
    compatibility). Falls back to raw little-endian float32 struct packing --
    same wire format. Layout: N x 4-byte IEEE 754 little-endian float32.

    The raw layout means deserialize() works without sqlite-vec present.
    """
    if _SQLITE_VEC_AVAILABLE and _sqlite_vec is not None:
        return _sqlite_vec.serialize_float32(vector)
    # Raw fallback: same byte layout as sqlite_vec.serialize_float32.
    return struct.pack(f"<{len(vector)}f", *vector)


def deserialize(blob: bytes) -> list[float]:
    """Deserialize a binary blob to a list of floats.

    Layout: N x 4-byte IEEE 754 little-endian float32 (same format as
    sqlite_vec.serialize_float32 and the raw struct fallback in serialize()).
    Works without sqlite-vec present.
    """
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _l2_norm(vector: list[float]) -> float:
    """Compute the L2 norm of a vector."""
    return math.sqrt(sum(x * x for x in vector))


def _normalize(vector: list[float]) -> list[float]:
    """Return a unit-norm copy of vector. Returns the zero vector if norm == 0."""
    norm = _l2_norm(vector)
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def renormalized_mean(vectors: list[list[float]]) -> list[float] | None:
    """Compute the arithmetic mean of vectors then L2-normalize the result.

    Re-normalization is required for cosine similarity: an unnormalized mean
    shrinks toward zero on diverse thread topics, breaking cosine scoring.

    Args:
        vectors: Non-empty list of equal-length float lists.

    Returns:
        Normalized mean vector, or None if vectors is empty.
    """
    if not vectors:
        return None
    dim = len(vectors[0])
    mean = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            mean[i] += v[i]
    n = len(vectors)
    mean = [x / n for x in mean]
    return _normalize(mean)


def incremental_centroid(
    old: list[float],
    n: int,
    post_vec: list[float],
) -> list[float]:
    """Incrementally update a thread centroid when a new post arrives.

    Formula: new = normalize((old * n + post_vec) / (n + 1))

    where n = number of already-embedded posts in the thread (NOT including
    the new post). Compute n by query:
        SELECT COUNT(*) FROM posts WHERE thread_id=? AND embedding IS NOT NULL
    -- never from a denormalized counter column (drift-proof under failed embeds).

    Re-normalization is applied to the result because the arithmetic mean of
    normalized vectors is not itself unit-norm (on diverse topics the mean
    shrinks toward zero, breaking cosine similarity).

    Args:
        old:      The current thread centroid vector (normalized).
        n:        Count of already-embedded posts in the thread.
        post_vec: The new post's embedding vector (normalized).

    Returns:
        New normalized centroid vector.
    """
    dim = len(old)
    # Weighted sum: old * n + post_vec, then divide by (n+1)
    updated = [(old[i] * n + post_vec[i]) / (n + 1) for i in range(dim)]
    return _normalize(updated)
