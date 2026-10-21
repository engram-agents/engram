"""health.rows -- row-factory-independent reads for the health package
(engram-alpha#3128).

Health code reads rows by column NAME (``row["c"]``, ``r["recall_summary"]``).
That only works when the connection's ``row_factory`` is ``sqlite3.Row``; a
plain ``sqlite3.connect(...)`` yields tuple rows and every named read raises
``TypeError``. Several callers hand health a plain connection (the dream
master's repair pass calls the signal functions on one it opened itself), so
the requirement must not live in the caller.

``named_rows`` is the one place that guarantees named access: every
name-indexed read on a caller-supplied connection goes through it.
"""

from __future__ import annotations

import sqlite3


def named_rows(conn: sqlite3.Connection, sql: str, params=()) -> sqlite3.Cursor:
    """Execute ``sql`` on a fresh cursor whose OWN ``row_factory`` is
    ``sqlite3.Row`` and return that cursor.

    Contract: rows from the returned cursor support access by column name
    regardless of ``conn.row_factory``; ``conn.row_factory`` itself is never
    read or written, so a shared connection (e.g. the server's) is left
    exactly as the caller configured it. Harmless on a connection that
    already uses ``sqlite3.Row``.
    """
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, params)
