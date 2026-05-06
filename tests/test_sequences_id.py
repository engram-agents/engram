"""Shared parity tests for the sequences-id-protocol.

Spec: active-work/sequences-id-protocol-shared-2026-04-29.md
Resolves: qu_0001 (ID-generation race)

These tests verify the SQL contract — schema, atomic increment, migration —
without importing server.py. Both Borges and Mneme run them against their
own DBs; if both pass, the parity floor is met. Per-side integration tests
of _next_id wiring are each side's responsibility.

Requires SQLite ≥3.35 (UPDATE...RETURNING, INSERT...ON CONFLICT...RETURNING).
"""

import sqlite3
import threading
from pathlib import Path

import pytest


# --- The SQL contract ----------------------------------------------------
# These three constants are the parity surface. Both server.py copies must
# emit these exact statements (or semantic equivalents) against their DBs.

CREATE_SEQUENCES_TABLE = """
CREATE TABLE IF NOT EXISTS sequences (
    node_type TEXT PRIMARY KEY,
    last_num INTEGER NOT NULL DEFAULT 0
);
"""

ATOMIC_INCREMENT = """
INSERT INTO sequences (node_type, last_num) VALUES (?, 1)
ON CONFLICT(node_type) DO UPDATE SET last_num = last_num + 1
RETURNING last_num;
"""

MIGRATION = """
INSERT INTO sequences (node_type, last_num)
SELECT
    SUBSTR(id, 1, INSTR(id, '_') - 1) AS prefix,
    MAX(CAST(SUBSTR(id, INSTR(id, '_') + 1) AS INTEGER)) AS max_num
FROM nodes
WHERE INSTR(id, '_') > 0
GROUP BY prefix
ON CONFLICT(node_type) DO UPDATE SET last_num = excluded.last_num
    WHERE excluded.last_num > sequences.last_num;
"""


# --- Fixtures ------------------------------------------------------------

@pytest.fixture
def conn(tmp_path: Path):
    """Fresh in-memory-ish DB with nodes + sequences tables, no seeded rows."""
    db_path = tmp_path / "test.db"
    c = sqlite3.connect(str(db_path), isolation_level=None)
    c.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    c.executescript(CREATE_SEQUENCES_TABLE)
    yield c
    c.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """DB path with schema initialized — for tests that open multiple connections."""
    p = tmp_path / "test.db"
    setup = sqlite3.connect(str(p))
    setup.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    setup.executescript(CREATE_SEQUENCES_TABLE)
    setup.commit()
    setup.close()
    return p


# --- Sanity check --------------------------------------------------------

def test_sqlite_version_supports_returning():
    """We require SQLite ≥3.35 for UPDATE...RETURNING. Fail loudly if older."""
    version = sqlite3.sqlite_version_info
    assert version >= (3, 35), (
        f"SQLite {sqlite3.sqlite_version} is too old for UPDATE...RETURNING. "
        f"Need ≥3.35."
    )


# --- Atomic increment ----------------------------------------------------

def test_atomic_increment_first_call_returns_one(conn):
    """First call for a new prefix creates the row at last_num=1."""
    cur = conn.execute(ATOMIC_INCREMENT, ('ob',))
    assert cur.fetchone()[0] == 1


def test_atomic_increment_subsequent_calls_increment(conn):
    """Subsequent calls increment monotonically."""
    for expected in range(1, 6):
        cur = conn.execute(ATOMIC_INCREMENT, ('ob',))
        assert cur.fetchone()[0] == expected


def test_atomic_increment_per_prefix_independent(conn):
    """Different prefixes maintain independent counters."""
    assert conn.execute(ATOMIC_INCREMENT, ('ob',)).fetchone()[0] == 1
    assert conn.execute(ATOMIC_INCREMENT, ('ob',)).fetchone()[0] == 2
    assert conn.execute(ATOMIC_INCREMENT, ('dv',)).fetchone()[0] == 1  # fresh
    assert conn.execute(ATOMIC_INCREMENT, ('ev',)).fetchone()[0] == 1  # fresh
    assert conn.execute(ATOMIC_INCREMENT, ('ob',)).fetchone()[0] == 3  # continues


# --- Concurrent insert (the race the protocol solves) -------------------

def test_concurrent_insert_no_duplicates(db_path: Path):
    """50 threads each call atomic increment; all 50 returned IDs unique."""
    n_threads = 50
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker():
        # Per-thread connection (sqlite3 connections aren't thread-safe to share)
        c = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        try:
            barrier.wait()  # release all threads simultaneously to maximize contention
            cur = c.execute(ATOMIC_INCREMENT, ('ob',))
            num = cur.fetchone()[0]
            with lock:
                results.append(num)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n_threads
    assert len(set(results)) == n_threads, f"Duplicate IDs found: {sorted(results)}"
    assert sorted(results) == list(range(1, n_threads + 1)), (
        f"Expected contiguous 1..{n_threads}, got {sorted(results)}"
    )


# --- Migration -----------------------------------------------------------

def test_migration_correctness(conn):
    """Migration sets last_num = MAX(numeric suffix) per prefix from nodes."""
    seed = [
        ('ob_0001',), ('ob_0002',), ('ob_0042',),
        ('dv_0001',), ('dv_0123',),
        ('ev_0007',),
    ]
    conn.executemany("INSERT INTO nodes (id) VALUES (?)", seed)

    conn.execute(MIGRATION)

    rows = dict(conn.execute("SELECT node_type, last_num FROM sequences").fetchall())
    assert rows == {'ob': 42, 'dv': 123, 'ev': 7}


def test_migration_idempotent(conn):
    """Running migration twice produces same end state."""
    seed = [('ob_0001',), ('ob_0050',), ('dv_0010',)]
    conn.executemany("INSERT INTO nodes (id) VALUES (?)", seed)

    conn.execute(MIGRATION)
    state_a = dict(conn.execute("SELECT node_type, last_num FROM sequences").fetchall())

    conn.execute(MIGRATION)
    state_b = dict(conn.execute("SELECT node_type, last_num FROM sequences").fetchall())

    assert state_a == state_b == {'ob': 50, 'dv': 10}


def test_migration_does_not_decrement(conn):
    """If sequences is already ahead of MAX(node id), migration leaves it alone."""
    conn.execute("INSERT INTO nodes (id) VALUES ('ob_0050')")
    # Increment sequences to 100 via the atomic statement (simulating 100 IDs handed out)
    for _ in range(100):
        conn.execute(ATOMIC_INCREMENT, ('ob',))
    assert conn.execute("SELECT last_num FROM sequences WHERE node_type='ob'").fetchone()[0] == 100

    conn.execute(MIGRATION)

    # Migration must NOT decrement back to 50 — that would mint duplicate IDs.
    assert conn.execute("SELECT last_num FROM sequences WHERE node_type='ob'").fetchone()[0] == 100


def test_migration_handles_empty_nodes(conn):
    """Migration on an empty nodes table is a no-op (no rows to insert)."""
    conn.execute(MIGRATION)
    rows = conn.execute("SELECT * FROM sequences").fetchall()
    assert rows == []


def test_migration_skips_malformed_ids(conn):
    """IDs without an underscore are excluded from the seed (defensive)."""
    seed = [('ob_0001',), ('legacy_old_format_no_num',), ('justplaintext',)]
    # Note: 'legacy_old_format_no_num' DOES have underscores, so it would be picked up
    # with prefix='legacy' and num=parse_failure. The CAST to INTEGER on a non-numeric
    # suffix yields 0 in SQLite, so this is a defensive check on real-world cleanliness.
    # 'justplaintext' has no underscore at all → INSTR returns 0 → WHERE INSTR>0 excludes it.
    conn.executemany("INSERT INTO nodes (id) VALUES (?)", seed)

    conn.execute(MIGRATION)

    rows = dict(conn.execute("SELECT node_type, last_num FROM sequences").fetchall())
    # 'ob' should be 1; 'legacy' may exist with last_num=0 due to CAST behavior;
    # 'justplaintext' must NOT appear.
    assert rows.get('ob') == 1
    assert 'justplaintext' not in rows


# --- End-to-end: migration + atomic increment compose ------------------

def test_post_migration_atomic_increment_continues_from_max(conn):
    """After migration, the next atomic increment returns max+1, not 1."""
    conn.executemany("INSERT INTO nodes (id) VALUES (?)", [('ob_0001',), ('ob_0042',)])

    conn.execute(MIGRATION)

    # Next ID after migration should be 43, not 1.
    cur = conn.execute(ATOMIC_INCREMENT, ('ob',))
    assert cur.fetchone()[0] == 43
