"""Single read-only SQLite opener for the viz package (engram-alpha#3513).

Why a helper: the old ``f"file:{db_path}?mode=ro"`` URI treated ``?`` and ``#``
in the path as URI delimiters, so ``mode=ro`` was lost and SQLite opened the
truncated path read-write-create -- silently leaving a stray empty file.
``Path.as_uri()`` percent-encodes ``?``, ``#``, ``%`` and spaces, so the whole
path stays the path. Every viz read-only open goes through here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

try:  # flat core module: installed plugin / src/engram on sys.path
    from engram_regular_io import StoreUnusable, check_store_path
except ImportError:  # source checkout with only `src` on PYTHONPATH: same file, package-qualified
    from engram.engram_regular_io import StoreUnusable, check_store_path


def connect_ro(db_path, *, immutable: bool = False,
               timeout: float | None = None) -> sqlite3.Connection:
    """Open *db_path* read-only. Raises ``sqlite3.OperationalError`` if the
    path is not a usable store (mode=ro never creates). The path is classified
    first, without opening it (#3962, ``engram_regular_io.check_store_path``): a
    missing path, an unreadable or looping one, and a FIFO / directory / socket /
    device (whose ``mode=ro`` open can block) each raise ``OperationalError``
    carrying the classifier's message, ``from`` the classifier's exception, so
    the callers' ``except sqlite3.OperationalError`` need not change.
    ``immutable=True`` adds ``immutable=1`` (no -shm/-wal sidecar writes; for
    WAL DBs read while a writer is live). ``timeout`` is passed to sqlite3
    only when given."""
    try:
        check_store_path(db_path)
    except (FileNotFoundError, StoreUnusable) as exc:
        raise sqlite3.OperationalError(str(exc)) from exc
    uri = Path(db_path).absolute().as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    kwargs = {"uri": True}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return sqlite3.connect(uri, **kwargs)
