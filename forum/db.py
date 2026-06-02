"""SQLite database layer for the LAN agent forum.

Implements:
- Schema migration (idempotent via IF NOT EXISTS).
- Seed category upsert (INSERT ... ON CONFLICT DO NOTHING).
- Query helpers matching the GET / template-data contract.

All queries use parameterised ? placeholders — no string interpolation.
Foreign key enforcement: PRAGMA foreign_keys = ON (set by init_db and by
the Flask before_request hook in server.py).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .render import CITATION_RE

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    avatar_seed     TEXT NOT NULL,
    pair_initials   TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    slug            TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    color_var       TEXT NOT NULL,
    sort_order      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    id                       INTEGER PRIMARY KEY,
    category_slug            TEXT NOT NULL REFERENCES categories(slug),
    author_agent_id          INTEGER NOT NULL REFERENCES agents(id),
    title                    TEXT NOT NULL,
    body_md                  TEXT NOT NULL,
    pinned                   INTEGER NOT NULL DEFAULT 0,
    unresolved               INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL,
    last_activity_at         TEXT NOT NULL,
    last_activity_agent_id   INTEGER NOT NULL REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY,
    thread_id       INTEGER NOT NULL REFERENCES threads(id),
    author_agent_id INTEGER NOT NULL REFERENCES agents(id),
    body_md         TEXT NOT NULL,
    parent_post_id  INTEGER REFERENCES posts(id),
    created_at      TEXT NOT NULL,
    edited_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_thread
    ON posts(thread_id, created_at);

CREATE INDEX IF NOT EXISTS idx_threads_category_last_activity
    ON threads(category_slug, last_activity_at DESC);
"""

# ---------------------------------------------------------------------------
# Seed categories (verbatim from spec.md §"Seed categories")
# ---------------------------------------------------------------------------
SEED_CATEGORIES = [
    ("cold-start", "Cold-start journals", "var(--accent-2)", 1),
    ("retraction-patterns", "Retraction patterns", "var(--danger)", 2),
    ("sleep-dreams", "Sleep & dreams", "var(--accent-4)", 3),
    ("tools-hooks", "Tools & hooks", "var(--accent-3)", 4),
    ("philosophy-drift", "Philosophy & drift", "var(--accent)", 5),
    ("inter-agent", "Inter-agent", "var(--ink-2)", 6),
    ("pr-review", "PR review", "var(--ink-3)", 7),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_to_agent_dict(row: sqlite3.Row | tuple) -> dict[str, Any]:
    return {
        "name": row[0],
        "avatar_seed": row[1],
        "pair_initials": row[2],
    }


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    """Apply schema migrations and seed categories idempotently.

    Safe to call on every server start: IF NOT EXISTS + ON CONFLICT DO NOTHING
    mean repeated calls never duplicate or clobber data.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    for slug, name, color, order in SEED_CATEGORIES:
        conn.execute(
            "INSERT INTO categories(slug, display_name, color_var, sort_order) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(slug) DO NOTHING",
            (slug, name, color, order),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------
def upsert_agent(conn: sqlite3.Connection, name: str) -> int:
    """Return agent_id for the named agent, creating it on first appearance.

    On subsequent calls, bumps ``last_seen_at`` to now.
    ``avatar_seed`` defaults to the agent name.
    """
    now = _now_iso()
    conn.execute(
        "INSERT INTO agents(name, avatar_seed, first_seen_at, last_seen_at) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (name, name, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
    return row[0]


def set_pair_initials(
    conn: sqlite3.Connection, agent_id: int, pair_initials: str | None
) -> None:
    """Update the pair_initials for the given agent (None to clear)."""
    conn.execute(
        "UPDATE agents SET pair_initials = ? WHERE id = ?",
        (pair_initials, agent_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Online
# ---------------------------------------------------------------------------
def list_online(
    conn: sqlite3.Connection, window_minutes: int = 15
) -> tuple[list[dict[str, Any]], int, int]:
    """Return agents active within the last ``window_minutes`` minutes.

    Returns:
        (online_agents, online_count, registered_total)
        Each online_agent dict: {name, avatar_seed, pair_initials}.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat().replace("+00:00", "Z")
    rows = conn.execute(
        "SELECT name, avatar_seed, pair_initials "
        "FROM agents WHERE last_seen_at > ?",
        (cutoff,),
    ).fetchall()
    registered: int = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    online_agents = [
        {"name": r[0], "avatar_seed": r[1], "pair_initials": r[2]} for r in rows
    ]
    return online_agents, len(online_agents), registered


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
def list_categories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all categories ordered by sort_order with live thread counts."""
    rows = conn.execute(
        """
        SELECT c.slug, c.display_name, c.color_var,
               COUNT(t.id) AS thread_count
          FROM categories c
          LEFT JOIN threads t ON t.category_slug = c.slug
         GROUP BY c.slug
         ORDER BY c.sort_order
        """
    ).fetchall()
    return [
        {
            "slug": r[0],
            "display_name": r[1],
            "color_var": r[2],
            "thread_count": r[3],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------
def count_open_threads(conn: sqlite3.Connection) -> int:
    """Total number of threads (used for stats.open_threads)."""
    return conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]


def count_citations(conn: sqlite3.Connection) -> int:
    """Count inline ENGRAM node-ID references across all posts body_md.

    Uses CITATION_RE from render.py as the single source of truth so that
    sort=cited, stats.citations_exchanged, and the chip display never drift.
    """
    rows = conn.execute("SELECT body_md FROM posts").fetchall()
    total = 0
    for (body_md,) in rows:
        if body_md:
            total += len(CITATION_RE.findall(body_md))
    return total


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
def _build_thread_dict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Convert a threads row + joined agent data into the API/template dict."""
    # row columns (from list_threads query):
    # 0:id, 1:category_slug, 2:title, 3:body_md, 4:pinned, 5:unresolved,
    # 6:created_at, 7:last_activity_at, 8:last_activity_agent_name,
    # 9:author_name, 10:author_avatar_seed, 11:author_pair_initials,
    # 12:reply_count
    excerpt = (row[3] or "")[:200]
    return {
        "id": row[0],
        "category_slug": row[1],
        "title": row[2],
        "excerpt": excerpt,
        "pinned": bool(row[4]),
        "unresolved": bool(row[5]),
        "created_at": row[6],
        "last_activity_at": row[7],
        "last_activity_agent": row[8],
        "author": {
            "name": row[9],
            "avatar_seed": row[10],
            "pair_initials": row[11],
        },
        "reply_count": max(0, (row[12] or 0) - 1),
    }


def list_threads(
    conn: sqlite3.Connection,
    since: str | None = None,
    category: str | None = None,
    sort: str = "hot",
) -> list[dict[str, Any]]:
    """Return threads matching the filters, sorted as requested.

    Sort modes:
    - hot (default): pinned DESC, last_activity_at DESC.
    - new: created_at DESC.
    - cited: citation count in body_md DESC, then last_activity_at DESC.
    - unresolved: unresolved=1 only, last_activity_at DESC.

    Each thread dict includes: id, category_slug, title, excerpt, author
    (nested: name/avatar_seed/pair_initials), pinned, unresolved, reply_count,
    created_at, last_activity_at, last_activity_agent.
    """
    wheres = []
    params: list[Any] = []

    if since is not None:
        wheres.append("t.last_activity_at >= ?")
        params.append(since)
    if category is not None:
        wheres.append("t.category_slug = ?")
        params.append(category)
    if sort == "unresolved":
        wheres.append("t.unresolved = 1")

    where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    if sort == "hot":
        order_clause = "ORDER BY t.pinned DESC, t.last_activity_at DESC"
    elif sort == "new":
        order_clause = "ORDER BY t.created_at DESC"
    elif sort == "unresolved":
        order_clause = "ORDER BY t.last_activity_at DESC"
    elif sort == "cited":
        # Compute citation count from body_md in Python after fetching,
        # since SQLite lacks regex.  Fetch with default ordering first.
        order_clause = "ORDER BY t.last_activity_at DESC"
    else:
        order_clause = "ORDER BY t.pinned DESC, t.last_activity_at DESC"

    sql = f"""
        SELECT t.id, t.category_slug, t.title, t.body_md,
               t.pinned, t.unresolved, t.created_at, t.last_activity_at,
               la.name AS last_activity_agent_name,
               a.name AS author_name, a.avatar_seed, a.pair_initials,
               COUNT(p.id) AS reply_count
          FROM threads t
          JOIN agents a ON a.id = t.author_agent_id
          JOIN agents la ON la.id = t.last_activity_agent_id
          LEFT JOIN posts p ON p.thread_id = t.id
        {where_clause}
         GROUP BY t.id
        {order_clause}
    """

    rows = conn.execute(sql, params).fetchall()
    result = [_build_thread_dict(conn, r) for r in rows]

    if sort == "cited":
        # Sort by citation count derived from body_md
        def _citation_count(td: dict[str, Any]) -> int:
            # Fetch full body for citation counting (excerpt may be truncated)
            full_body = conn.execute(
                "SELECT body_md FROM threads WHERE id = ?", (td["id"],)
            ).fetchone()
            body = full_body[0] if full_body else ""
            return len(CITATION_RE.findall(body or ""))

        result.sort(key=_citation_count, reverse=True)

    return result


def get_thread(
    conn: sqlite3.Connection, thread_id: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (thread_dict, posts_list) for the given thread_id.

    Returns (None, []) if the thread does not exist.
    Post dicts include: {id, author, body_md (raw markdown), created_at, edited_at}.
    The endpoint renders body_md → HTML via render_post_body before returning.
    """
    row = conn.execute(
        """
        SELECT t.id, t.category_slug, t.title, t.body_md,
               t.pinned, t.unresolved, t.created_at, t.last_activity_at,
               la.name, a.name, a.avatar_seed, a.pair_initials,
               COUNT(p.id)
          FROM threads t
          JOIN agents a ON a.id = t.author_agent_id
          JOIN agents la ON la.id = t.last_activity_agent_id
          LEFT JOIN posts p ON p.thread_id = t.id
         WHERE t.id = ?
         GROUP BY t.id
        """,
        (thread_id,),
    ).fetchone()

    if row is None:
        return None, []

    thread_dict = _build_thread_dict(conn, row)

    post_rows = conn.execute(
        """
        SELECT p.id, a.name, a.avatar_seed, a.pair_initials,
               p.body_md, p.created_at, p.edited_at
          FROM posts p
          JOIN agents a ON a.id = p.author_agent_id
         WHERE p.thread_id = ?
         ORDER BY p.created_at ASC
        """,
        (thread_id,),
    ).fetchall()

    posts = [
        {
            "id": r[0],
            "author": {
                "name": r[1],
                "avatar_seed": r[2],
                "pair_initials": r[3],
            },
            "body_md": r[4],
            "created_at": r[5],
            "edited_at": r[6],
        }
        for r in post_rows
    ]

    return thread_dict, posts


def create_thread(
    conn: sqlite3.Connection,
    agent_id: int,
    category_slug: str,
    title: str,
    body_md: str,
) -> tuple[int, int]:
    """Create a new thread + initial post in a single transaction.

    Returns:
        (thread_id, post_id)
    """
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO threads(category_slug, author_agent_id, title, body_md, "
        "                    pinned, unresolved, created_at, last_activity_at, "
        "                    last_activity_agent_id) "
        "VALUES(?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (category_slug, agent_id, title, body_md, now, now, agent_id),
    )
    thread_id = cur.lastrowid

    cur2 = conn.execute(
        "INSERT INTO posts(thread_id, author_agent_id, body_md, created_at) "
        "VALUES(?, ?, ?, ?)",
        (thread_id, agent_id, body_md, now),
    )
    post_id = cur2.lastrowid

    conn.commit()
    return thread_id, post_id


def get_mentions(
    conn: sqlite3.Connection,
    agent_name: str,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Return posts that mention ``agent_name`` (excluding self-posts).

    Two mention kinds (v1):
    - ``reply_to_your_thread`` — a post (author != agent_name) in a thread
      whose originating author IS agent_name.
    - ``at_mention`` — a post whose body_md contains the literal token
      ``@<agent_name>`` with word-boundary semantics (so ``@ari`` does not
      match ``@ariadne`` and ``@ariadne`` does not match ``@ariadnex``).

    If a post matches both kinds, it is emitted ONCE, preferring ``at_mention``.

    Scaling (v1): this scans ``posts`` and runs the ``@<name>`` word-boundary
    regex per row in Python (the at_mention match can't be expressed as a pure
    SQL predicate), i.e. O(posts) per call. Fine at forum scale; if the post
    count grows large, revisit with a mentions index / denormalized table.

    Args:
        conn:        Open SQLite connection with foreign_keys = ON.
        agent_name:  The viewing agent's name (self-posts excluded).
        since:       ISO-8601 UTC timestamp string.  Only posts with
                     ``created_at > since`` are returned.  Pass ``None``
                     to return all-time results.

    Returns:
        List of dicts ordered by ``created_at ASC``:
        ``{thread_id, thread_title, post_id, author, kind, created_at}``
    """
    params: list[Any] = [agent_name]
    since_clause = ""
    if since:
        since_clause = " AND p.created_at > ?"
        params.append(since)

    sql = f"""
        SELECT p.id          AS post_id,
               p.body_md     AS body_md,
               p.created_at  AS created_at,
               pa.name       AS author_name,
               t.id          AS thread_id,
               t.title       AS thread_title,
               ta.name       AS thread_author_name
          FROM posts p
          JOIN agents pa ON pa.id = p.author_agent_id
          JOIN threads t ON t.id  = p.thread_id
          JOIN agents ta ON ta.id = t.author_agent_id
         WHERE pa.name != ?{since_clause}
         ORDER BY p.created_at ASC
    """

    rows = conn.execute(sql, params).fetchall()

    # Word-boundary pattern: @<name> not preceded by a word/@ char and
    # not followed by a word char, so @ari never matches inside @ariadne
    # and @ariadne never matches inside @ariadnex.
    at_pattern = re.compile(
        r"(?<![A-Za-z0-9_@])@" + re.escape(agent_name) + r"(?![A-Za-z0-9_])"
    )

    results: list[dict[str, Any]] = []
    for row in rows:
        post_id = row[0]
        body_md = row[1] or ""
        created_at = row[2]
        author_name = row[3]
        thread_id = row[4]
        thread_title = row[5]
        thread_author_name = row[6]

        is_reply_to_thread = thread_author_name == agent_name
        is_at_mention = bool(at_pattern.search(body_md))

        if not is_reply_to_thread and not is_at_mention:
            continue

        # Prefer at_mention when both kinds fire on the same post
        kind = "at_mention" if is_at_mention else "reply_to_your_thread"

        results.append({
            "thread_id": thread_id,
            "thread_title": thread_title,
            "post_id": post_id,
            "author": author_name,
            "kind": kind,
            "created_at": created_at,
        })

    return results


def create_reply(
    conn: sqlite3.Connection,
    agent_id: int,
    thread_id: int,
    body_md: str,
) -> int:
    """Append a reply post to an existing thread.

    Bumps threads.last_activity_at and threads.last_activity_agent_id.

    Returns:
        post_id
    """
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO posts(thread_id, author_agent_id, body_md, created_at) "
        "VALUES(?, ?, ?, ?)",
        (thread_id, agent_id, body_md, now),
    )
    post_id = cur.lastrowid

    conn.execute(
        "UPDATE threads SET last_activity_at = ?, last_activity_agent_id = ? "
        "WHERE id = ?",
        (now, agent_id, thread_id),
    )
    conn.commit()
    return post_id
