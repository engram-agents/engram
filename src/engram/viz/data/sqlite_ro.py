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


def connect_ro(db_path, *, immutable: bool = False,
               timeout: float | None = None) -> sqlite3.Connection:
    """Open *db_path* read-only. Raises ``sqlite3.OperationalError`` if the
    file does not exist (mode=ro never creates). ``immutable=True`` adds
    ``immutable=1`` (no -shm/-wal sidecar writes; for WAL DBs read while a
    writer is live). ``timeout`` is passed to sqlite3 only when given."""
    uri = Path(db_path).absolute().as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    kwargs = {"uri": True}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return sqlite3.connect(uri, **kwargs)
