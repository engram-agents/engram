"""One shared way for owner tests to give a store the schema version the owner requires (#3753).

`EngramdOwner.start()` refuses a store whose `PRAGMA user_version` is below
`engram_core.LATEST_SCHEMA_VERSION` (or that does not exist). Test stores built by hand
(`CREATE TABLE nodes ...`) have user_version 0, so every fixture that serves one to a real
owner stamps it through here. The version is read from engram_core's constant, never spelled
as a number, so the next migration rung is one line (that constant) and no test churns.

Order matters: a SQLite `Connection.backup()` onto a file REPLACES its header, user_version
included, so stamp AFTER any backup-into-file.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402


def stamp_current_schema(db_path, version=None) -> Path:
    """Set `PRAGMA user_version` on `db_path` (created empty if absent, no tables added) to
    `version`, default engram_core.LATEST_SCHEMA_VERSION. Returns the path."""
    db_path = Path(db_path)
    if version is None:
        version = engram_core.LATEST_SCHEMA_VERSION
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
    finally:
        conn.close()
    return db_path


def seed_current_store(home) -> Path:
    """A `knowledge.db` under `home` with no tables, stamped at the latest schema version: the
    least a fixture needs for the owner to start. Tests that need tables create them after."""
    return stamp_current_schema(Path(home) / "knowledge.db")


def stamp_if_behind(db_path) -> bool:
    """Stamp `db_path` at the latest schema version ONLY if it is an existing, non-empty regular
    SQLite file that already has tables and a user_version below the latest. Anything else
    (absent, 0 bytes, not SQLite, a directory/FIFO, already current or newer) is left alone, so
    the real gate still refuses what it should. Returns True if it stamped.

    This is what the conftest default (`_owner_gate_sees_current_store`) calls: it rescues a
    hand-built `CREATE TABLE ...` store at user_version 0 without ever inventing a store."""
    import os
    import stat

    p = Path(db_path)
    try:
        st = os.stat(p)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        return False
    try:
        conn = sqlite3.connect(str(p))
        try:
            behind = conn.execute("PRAGMA user_version").fetchone()[0] < engram_core.LATEST_SCHEMA_VERSION
            has_tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if behind and has_tables:
        stamp_current_schema(p)
        return True
    return False
