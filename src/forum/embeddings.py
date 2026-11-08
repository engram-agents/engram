"""Forum embedding layer — sentence-transformers wrapper + pure-math helpers.

Mirrors the engram server's degradation discipline (server.py:47-60):
loud, not silent. If sentence-transformers or sqlite-vec is missing the
forum degrades gracefully — FTS/LIKE search still works — but the operator
is told about it.

Env gate:
    FORUM_NO_EMBEDDINGS=1  — disables the embedding layer entirely (test mode;
                             mirrors ENGRAM_NO_EMBEDDINGS in server.py:108).

Model placement (mirrors #3905 / #3898 on the ENGRAM side; read at LOAD time):
    FORUM_EMBEDDING_DEVICE=cpu|cuda|cuda:N
                           — device the model loads on. Unset or empty means
                             "cpu". Matching is exact and case-sensitive (no
                             stripping); any other value refuses the load with a
                             named ForumEmbeddingConfigError, so the forum
                             degrades to FTS/LIKE and says why on stderr.
    FORUM_EMBEDDING_CPU_THREADS=N
                           — torch intra-op thread cap applied once per
                             successful load, only when the loaded model is on
                             CPU. Unset means 4; an invalid value (not an
                             integer from 1 to 1024) prints one stderr line and
                             uses 4.

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

import hashlib
import math
import os
import re
import struct
import sys
from pathlib import Path
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

# #4468 (part of #4447): the forum loads the SAME model at the SAME pinned
# revision as engram_core and verifies the same ten sha256 values. This is a
# deliberate COPY of engram_core._MINILM_PIN: the forum is deployed from
# src/forum/ alone and does not import engram_core. The root-suite drift cell
# tests/test_embedding_pin_4468.py is the one binding between the two tables;
# change both in the same commit. Provenance: the table covers the ten files
# the loader reads; the model.safetensors hash is the Hugging Face LFS object
# id from the HF API; the other nine were hashed from a local snapshot of that
# revision.
_MINILM_PIN = {
    "repo_id": "sentence-transformers/all-MiniLM-L6-v2",
    "revision": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "sha256": {
        "model.safetensors": "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db",
        "config.json": "953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41",
        "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
        "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
        "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
        "sentence_bert_config.json": "fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7",
        "1_Pooling/config.json": "4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23",
        "config_sentence_transformers.json": "061ca9d39661d6c6d6de5ba27f79a1cd5770ea247f8d46412a68a498dc5ac9f3",
        "special_tokens_map.json": "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3",
        "tokenizer_config.json": "acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b",
    },
}

# True once a load was refused (checksum mismatch, missing file, unresolvable
# snapshot). _get_model() runs per request, so a refusal is remembered here;
# without it every encode() would reload and re-hash about 90 MB. A refusal is
# cleared ONLY by a service restart (re-hashing per request is the
# alternative): an operator who sees the refusal line fixes the cache (or the
# pinned revision) and restarts the forum.
_model_refused: bool = False


# The snapshot directory whose ten files were hashed and matched in this
# process (absolute path), so a retry after a failed constructor does not
# re-hash. None until a verification passes.
_verified_snapshot: str | None = None


def _resolve_pinned_snapshot(online: bool = False) -> Path:
    """Return the ABSOLUTE path of the pinned revision's snapshot directory.

    online=False asks the local HF cache only (local_files_only=True, never
    touches the network) and raises if the revision is not cached;
    online=True downloads the pinned revision. The caller verifies the
    directory and then hands THIS path to the constructor.
    """
    from huggingface_hub import snapshot_download  # noqa: WPS433
    pin = _MINILM_PIN
    if online:
        # Only the ten pinned files: the repo holds 30 files (~977 MB,
        # pickle-capable ones included), so the download is limited to the
        # table. (The verifier separately refuses a directory holding any
        # other file; see _unexpected_entry.) huggingface_hub matches these with
        # fnmatch against repo-relative paths, so an exact name such as
        # "1_Pooling/config.json" matches only itself.
        kwargs = {"allow_patterns": list(pin["sha256"])}
    else:
        kwargs = {"local_files_only": True}
    return Path(os.path.abspath(
        snapshot_download(pin["repo_id"], revision=pin["revision"], **kwargs)
    ))


# A verification reason that starts with this means a pinned file is missing or
# unreadable (an interrupted download), as opposed to a checksum mismatch.
_INCOMPLETE_PREFIX = "snapshot file "


# The only files the verified directory may hold: the pinned names plus
# exactly a top-level README.md (the old by-name constructor left one in
# existing caches; sentence-transformers 5.3.0 reads it as UTF-8 text into its
# model-card text and never executes or parses it as configuration, but it is
# NOT hashed).
_ALLOWED_EXTRA_FILES = frozenset({"README.md"})


def _unexpected_entry(snap, pinned_names) -> "str | None":
    """Return a one-line reason for the first FILE under `snap` whose
    posix-relative path is not a pinned name or README.md, else None.
    Walks recursively and follows symlinked directories (snapshot entries are
    symlinks into blobs, and a linked directory must not hide a file); a
    directory already visited (a symlink cycle) is not walked twice. That
    also skips an alias to an already-walked directory, which is harmless:
    the loader reads only the fixed pinned relative paths, and the
    first-walked path of that directory was fully checked. The skip is
    order-dependent in the closed direction: an alias that sorts BEFORE its
    target is the first-walked path, and its files, under the alias's name,
    are unexpected and refused. Empty directories are harmless. A directory that cannot be listed is a reason
    too. The reason does NOT start with the missing-file prefix, so it is
    never healed by an online download."""
    allowed = set(pinned_names) | _ALLOWED_EXTRA_FILES
    errors = []
    seen = set()
    for dirpath, dirs, files in os.walk(snap, followlinks=True,
                                        onerror=errors.append):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs.sort()
        for name in sorted(files):
            rel = Path(os.path.relpath(os.path.join(dirpath, name), snap)).as_posix()
            if rel not in allowed:
                return (f"unexpected file {rel} in the snapshot directory "
                        f"(allowed: the ten pinned files and README.md)")
    if errors:
        return f"cannot list a snapshot directory ({errors[0]})"
    return None


def _verify_pinned_snapshot(snap: Path) -> str | None:
    """Check the directory's entry set, then hash the ten pinned files under
    `snap`; return None if all is as pinned, else a one-line reason naming the
    file and, for a mismatch, the expected and actual sha256 in full. Never
    touches the network. The entry check runs FIRST: a directory holding an
    unexpected file (the loader would read it: e.g. adapter_config.json
    switches transformers to its PEFT branch) is refused without hashing
    about 90 MB."""
    pin = _MINILM_PIN
    reason = _unexpected_entry(snap, pin["sha256"])
    if reason is not None:
        return reason
    for rel, expected in pin["sha256"].items():
        h = hashlib.sha256()
        try:
            with open(snap / rel, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError as e:
            return f"snapshot file {rel} is unreadable or missing ({e})"
        actual = h.hexdigest()
        if actual != expected:
            return (f"checksum mismatch for {rel}: expected sha256 "
                    f"{expected} but the file hashes to {actual}")
    return None


# ---------------------------------------------------------------------------
# Module-level model singleton
# ---------------------------------------------------------------------------

_model: Any = None  # SentenceTransformer | None


class ForumEmbeddingConfigError(ValueError):
    """FORUM_EMBEDDING_DEVICE holds a value that is not cpu | cuda | cuda:N."""


_DEVICE_RE = re.compile(r"^(cpu|cuda(:[0-9]+)?)$")
_DEFAULT_CPU_THREADS = 4
_MAX_CPU_THREADS = 1024


def _embedding_device() -> str:
    """Resolve FORUM_EMBEDDING_DEVICE at load time (unset or empty -> "cpu").

    Exact, case-sensitive match; no stripping. Raises ForumEmbeddingConfigError
    for anything else.
    """
    value = os.environ.get("FORUM_EMBEDDING_DEVICE", "")
    if value == "":
        return "cpu"
    if not _DEVICE_RE.fullmatch(value):
        raise ForumEmbeddingConfigError(
            f"FORUM_EMBEDDING_DEVICE={value!r} is not valid; allowed: "
            f"'cpu', 'cuda', or 'cuda:N' (N digits), exact and case-sensitive"
        )
    return value


def _embedding_cpu_threads() -> int:
    """Resolve FORUM_EMBEDDING_CPU_THREADS at load time (unset -> 4).

    Accepts ASCII digits only, 1 to 4 characters, with value 1.._MAX_CPU_THREADS
    (1024). Anything else does not refuse (thread count is a performance knob)
    and never raises: one stderr line, then the default. The length check comes
    before int(), so int() never sees a long string (CPython 3.12 raises
    ValueError past 4300 digits). Mirrors engram_core._embedding_device_settings.
    """
    raw = os.environ.get("FORUM_EMBEDDING_CPU_THREADS")
    if raw is None:
        return _DEFAULT_CPU_THREADS
    if re.fullmatch(r"[0-9]{1,4}", raw) and 1 <= int(raw) <= _MAX_CPU_THREADS:
        return int(raw)
    shown = repr(raw[:40])
    note = f" (value truncated; length {len(raw)})" if len(raw) > 40 else ""
    print(
        f"[forum] FORUM_EMBEDDING_CPU_THREADS={shown} is not an integer from "
        f"1 to {_MAX_CPU_THREADS}{note}; using {_DEFAULT_CPU_THREADS}",
        file=sys.stderr,
    )
    return _DEFAULT_CPU_THREADS


def _cap_cpu_threads(model: Any, threads: int) -> None:
    """Apply torch.set_num_threads(threads) iff the LOADED model is on CPU.

    Duplicate of engram_core._cap_cpu_threads (the original; forum does not
    import engram_core). A model that reports no device is left alone.
    """
    dev = getattr(model, "device", None)
    if dev is None:
        dev = getattr(getattr(model, "model", None), "device", None)
    if dev is None:
        return
    dev_type = getattr(dev, "type", None) or str(dev).split(":")[0]
    if dev_type != "cpu":
        return
    try:
        import torch  # noqa: WPS433
        torch.set_num_threads(threads)
    except Exception:
        pass


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

    #4468 (part of #4447): the pinned revision's snapshot directory is
    resolved first (local cache; on a miss, one online snapshot_download of
    the pinned revision), its ten files are sha256-verified once, and only
    then is the constructor handed the verified ABSOLUTE snapshot path (no
    revision=, no model name), so neither the working directory nor
    SENTENCE_TRANSFORMERS_HOME can redirect the load: the verified bytes are
    the loaded bytes: the ten pinned files are hashed, and the directory's
    entry set is checked to be those ten plus a top-level README.md (which is
    not hashed; sentence-transformers reads it as text only), before the
    constructor runs. A
    refusal sets _model_refused so later calls return None without
    reloading, and prints one stderr line naming the file and both hashes;
    there is no fallback to an unpinned load, and the forum then runs with
    the embedding layer off (FTS/LIKE search still works). One residual: a
    file replaced or added between the check and construction (same-user
    write access) is not covered. A cache holding an older revision does one online
    download, and an offline host lacking the pinned revision loses semantic
    search until it is online once.

    A refusal is cleared only by a service restart, because re-hashing about
    90 MB per request is the alternative: an operator who sees the refusal
    line fixes the cache (or the pinned revision) and restarts the forum. Any
    exception while VERIFYING is a remembered refusal; a failure to construct,
    to cap threads, or to download is retryable (a retry reuses the verified
    path without re-hashing). A cache-miss download fetches only the ten
    pinned files. If the local snapshot is missing or cannot read a pinned
    file (an interrupted first download), ONE online resolve completes it and
    it is verified again; a checksum MISMATCH is refused at once and is never
    healed by downloading over it.

    Concurrency: concurrent first calls may each load and verify; every path
    assigns only a verified model and two refusals set the same flag, and
    warm_model() runs at create_app before requests arrive. No lock.
    """
    global _model, _model_refused, _verified_snapshot
    if os.environ.get("FORUM_NO_EMBEDDINGS"):
        return None
    if not _ST_AVAILABLE:
        return None
    if _model_refused:
        return None
    if _model is None:
        try:
            dev = _embedding_device()
            threads = _embedding_cpu_threads()
            snap_path = _verified_snapshot
            if snap_path is None:
                online_done = False
                try:
                    # #1762: offline-first. A cached snapshot resolves with
                    # ZERO HuggingFace Hub network traffic -- no online
                    # etag-check, so the ~83s stall (HF Hub slow/unreachable)
                    # cannot happen on the common warm-cache path.
                    snap = _resolve_pinned_snapshot()
                except Exception:
                    # Cache-miss: this install does NOT pre-download the
                    # embedder model as a discrete install step. Fall back
                    # to a one-time online download of the PINNED revision
                    # so a fresh install keeps working; a failure here is a
                    # load failure (outer handler: retryable, not
                    # remembered), exactly as the online constructor was.
                    print(
                        f"[forum] Embedding model '{FORUM_EMBEDDING_MODEL}' not in "
                        f"local HF cache -- downloading once (online); subsequent "
                        f"loads are offline.",
                        file=sys.stderr,
                    )
                    snap = _resolve_pinned_snapshot(online=True)
                    online_done = True

                def _checked(snap_dir):
                    try:
                        return _verify_pinned_snapshot(snap_dir)
                    except Exception as exc:
                        # Unexpected failure while verifying: remember it, or
                        # every request would re-hash about 90 MB.
                        return (f"unexpected {type(exc).__name__} while "
                                f"verifying ({exc})")

                problem = _checked(snap)
                if (problem is not None and not online_done
                        and problem.startswith(_INCOMPLETE_PREFIX)):
                    # A partially populated snapshot (interrupted first
                    # download): complete it once, online, and verify again.
                    # A failure of this download is a load failure (outer
                    # handler: retryable). A mismatch never gets here.
                    print(
                        f"[forum] Embedding model '{FORUM_EMBEDDING_MODEL}' "
                        f"snapshot is incomplete ({problem}) -- completing "
                        f"it once (online).",
                        file=sys.stderr,
                    )
                    snap = _resolve_pinned_snapshot(online=True)
                    problem = _checked(snap)
                if problem is not None:
                    print(
                        f"[forum] Refusing embedding model "
                        f"'{FORUM_EMBEDDING_MODEL}' (pinned revision "
                        f"{_MINILM_PIN['revision']}): {problem}; the refusal "
                        f"lasts until restart -- fix the cache or the pinned "
                        f"revision, then restart the forum",
                        file=sys.stderr,
                    )
                    _model = None
                    _model_refused = True
                    return None
                snap_path = str(snap)
                _verified_snapshot = snap_path
            # Construct FROM the verified absolute path, never by name.
            model = _SentenceTransformer(snap_path, device=dev)
            _cap_cpu_threads(model, threads)
            _model = model
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
