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

import json
import os
import re
import sqlite3
import sys
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
    sort_order      INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'discussion'
);

CREATE TABLE IF NOT EXISTS threads (
    id                          INTEGER PRIMARY KEY,
    category_slug               TEXT NOT NULL REFERENCES categories(slug),
    author_agent_id             INTEGER NOT NULL REFERENCES agents(id),
    title                       TEXT NOT NULL,
    body_md                     TEXT NOT NULL,
    pinned                      INTEGER NOT NULL DEFAULT 0,
    unresolved                  INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT NOT NULL,
    last_activity_at            TEXT NOT NULL,
    last_activity_agent_id      INTEGER NOT NULL REFERENCES agents(id),
    accepted_answer_post_id     INTEGER REFERENCES posts(id)
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

CREATE TABLE IF NOT EXISTS post_verifications (
    id                 INTEGER PRIMARY KEY,
    post_id            INTEGER NOT NULL REFERENCES posts(id),
    verifier_agent_id  INTEGER NOT NULL REFERENCES agents(id),
    note               TEXT NOT NULL CHECK(length(trim(note)) > 0),
    created_at         TEXT NOT NULL,
    UNIQUE(post_id, verifier_agent_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_thread
    ON posts(thread_id, created_at);

CREATE INDEX IF NOT EXISTS idx_threads_category_last_activity
    ON threads(category_slug, last_activity_at DESC);

CREATE INDEX IF NOT EXISTS idx_post_verifications_post
    ON post_verifications(post_id);

CREATE TABLE IF NOT EXISTS reads (
    agent_id           INTEGER NOT NULL REFERENCES agents(id),
    thread_id          INTEGER NOT NULL REFERENCES threads(id),
    last_read_post_id  INTEGER NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (agent_id, thread_id)
);
"""

# ---------------------------------------------------------------------------
# Seed categories — emergency in-code fallback (last resort)
# Normal seeding uses load_category_config(); this constant is only used
# when no config file resolves at all.
# ---------------------------------------------------------------------------

# Valid category kind values.
CATEGORY_KINDS = ("discussion", "qa")

# 5-tuples: (slug, display_name, color_var, sort_order, kind)
SEED_CATEGORIES = [
    ("cold-start",          "Cold-start journals", "var(--accent-2)", 1, "discussion"),
    ("retraction-patterns", "Retraction patterns", "var(--danger)",   2, "discussion"),
    ("sleep-dreams",        "Sleep & dreams",       "var(--accent-4)", 3, "discussion"),
    ("tools-hooks",         "Tools & hooks",        "var(--accent-3)", 4, "discussion"),
    ("philosophy-drift",    "Philosophy & drift",   "var(--accent)",   5, "discussion"),
    ("inter-agent",         "Inter-agent",          "var(--ink-2)",    6, "discussion"),
    ("pr-review",           "PR review",            "var(--ink-3)",    7, "discussion"),
    ("q-and-a",             "Q&A",                  "var(--ink-4)",    8, "qa"),
]

# Path to the shipped default categories JSON, relative to this file's directory.
_DEFAULT_CATEGORIES_JSON = os.path.join(
    os.path.dirname(__file__), "seeds", "categories.default.json"
)


def load_category_config(path: str | None = None) -> list[dict]:
    """Load category definitions from config, with a resolution chain.

    Resolution order (first that exists/succeeds wins):
    1. explicit ``path`` argument
    2. ``FORUM_CATEGORIES_CONFIG`` environment variable
    3. ``~/.forum/categories.json`` (user override)
    4. shipped default ``forum/seeds/categories.default.json``
    5. in-code ``SEED_CATEGORIES`` emergency fallback (emits a stderr warning)

    Each entry must have: slug, display_name, color_var, sort_order.
    ``kind`` is optional and defaults to ``'discussion'``.
    Unknown ``kind`` values raise ``ValueError``.

    Returns:
        List of category dicts with keys: slug, display_name, color_var,
        sort_order, kind.
    """
    candidates: list[str | None] = [
        path,
        os.environ.get("FORUM_CATEGORIES_CONFIG"),
        os.path.expanduser("~/.forum/categories.json"),
        _DEFAULT_CATEGORIES_JSON,
    ]

    for candidate in candidates:
        if candidate is None:
            continue
        try:
            with open(candidate, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            # Any candidate that is present but unreadable/invalid is a hard error;
            # only absence (FileNotFoundError, above) is a silent skip to the next
            # candidate. Holds for every candidate — explicit arg, env, the user
            # override (~/.forum/categories.json), and a corrupt shipped default
            # (a packaging error worth failing loudly on). Emit which file failed.
            raise ValueError(
                f"Failed to parse category config {candidate!r}: {exc}"
            ) from exc

        # Validate and normalise entries.
        result: list[dict] = []
        for entry in raw:
            for required_key in ("slug", "display_name", "color_var", "sort_order"):
                if required_key not in entry:
                    raise ValueError(
                        f"Category entry missing required key {required_key!r}: {entry!r}"
                    )
            kind = entry.get("kind", "discussion")
            if kind not in CATEGORY_KINDS:
                raise ValueError(
                    f"Category {entry['slug']!r} has unknown kind {kind!r}; "
                    f"valid kinds: {CATEGORY_KINDS}"
                )
            result.append({
                "slug": entry["slug"],
                "display_name": entry["display_name"],
                "color_var": entry["color_var"],
                "sort_order": entry["sort_order"],
                "kind": kind,
            })
        return result

    # Emergency fallback: nothing resolved.
    print(
        "WARNING: forum category config not found; falling back to built-in SEED_CATEGORIES.",
        file=sys.stderr,
    )
    return [
        {
            "slug": slug,
            "display_name": name,
            "color_var": color,
            "sort_order": order,
            "kind": kind,
        }
        for slug, name, color, order, kind in SEED_CATEGORIES
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_to_agent_dict(row: sqlite3.Row | tuple) -> dict[str, Any]:
    return {
        "name": row[0],
        "avatar_seed": row[1],
        "pair_initials": row[2],
    }


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection, categories_config: str | None = None) -> None:
    """Apply schema migrations and seed categories idempotently.

    Safe to call on every server start: IF NOT EXISTS + ON CONFLICT DO NOTHING
    mean repeated calls never duplicate or clobber data.

    Args:
        categories_config: Optional path to a JSON categories config file.
            When None, load_category_config() resolution chain applies
            (env var → user override → shipped default → emergency fallback).
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)

    # Idempotent ALTER guard for threads.accepted_answer_post_id (pre-QA-slice DBs).
    # SQLite ALTER TABLE ADD COLUMN errors if the column exists, so we check
    # via PRAGMA before issuing the ALTER.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "accepted_answer_post_id" not in cols:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN "
            "accepted_answer_post_id INTEGER REFERENCES posts(id)"
        )

    # Idempotent ALTER guard for categories.kind (pre-configurable-categories DBs).
    cat_cols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
    if "kind" not in cat_cols:
        conn.execute(
            "ALTER TABLE categories ADD COLUMN kind TEXT NOT NULL DEFAULT 'discussion'"
        )
        # One-time backfill: existing q-and-a rows get kind='qa'. The `AND kind!='qa'`
        # guard is vacuous on the normal path (the ALTER just defaulted every row to
        # 'discussion') but is kept deliberately: it keeps the UPDATE idempotent and
        # a no-op if the column was ever added out-of-band with kind already set.
        conn.execute(
            "UPDATE categories SET kind='qa' WHERE slug='q-and-a' AND kind!='qa'"
        )

    categories = load_category_config(categories_config)
    for c in categories:
        conn.execute(
            "INSERT INTO categories(slug, display_name, color_var, sort_order, kind) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(slug) DO NOTHING",
            (c["slug"], c["display_name"], c["color_var"], c["sort_order"], c["kind"]),
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
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
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

def category_kind(conn: sqlite3.Connection, slug: str) -> str:
    """Return the kind of the named category, or 'discussion' if not found.

    Used by create_thread and accept_answer to key q-a semantics on the
    category's kind field rather than the literal slug 'q-and-a'.
    """
    row = conn.execute(
        "SELECT kind FROM categories WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        return "discussion"
    return row[0]


def list_categories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all categories ordered by sort_order with live thread counts."""
    rows = conn.execute(
        """
        SELECT c.slug, c.display_name, c.color_var, c.kind,
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
            "kind": r[3],
            "thread_count": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Category CRUD helpers (operator admin)
# ---------------------------------------------------------------------------

# Slug validation pattern: lowercase letters, digits, hyphens only.
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def add_category(
    conn: sqlite3.Connection,
    slug: str,
    display_name: str,
    color_var: str,
    sort_order: int,
    kind: str = "discussion",
) -> None:
    """Insert a new category row.

    Validates:
    - ``slug`` matches ``^[a-z0-9-]+$``.
    - ``kind`` is in CATEGORY_KINDS.
    - ``sort_order`` is an int.
    - Duplicate slug raises ``ForumConflict``.

    Note: Adding optional attributes (e.g. turn_tracked from #696) slots in as
    additional keyword parameters here and in the INSERT, without restructuring.
    """
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"slug {slug!r} is invalid; must match ^[a-z0-9-]+$ (lowercase, digits, hyphens only)"
        )
    if kind not in CATEGORY_KINDS:
        raise ValueError(
            f"kind {kind!r} is not valid; must be one of {CATEGORY_KINDS}"
        )
    if not isinstance(sort_order, int):
        raise ValueError(
            f"sort_order must be an int, got {type(sort_order).__name__!r}"
        )
    existing = conn.execute(
        "SELECT 1 FROM categories WHERE slug = ?", (slug,)
    ).fetchone()
    if existing is not None:
        raise ForumConflict(f"category {slug!r} already exists")
    conn.execute(
        "INSERT INTO categories(slug, display_name, color_var, sort_order, kind) "
        "VALUES(?, ?, ?, ?, ?)",
        (slug, display_name, color_var, sort_order, kind),
    )
    conn.commit()


def update_category(
    conn: sqlite3.Connection,
    slug: str,
    *,
    display_name: str | None = None,
    color_var: str | None = None,
    sort_order: int | None = None,
) -> None:
    """Update display fields on an existing category.

    Updates only the provided (non-None) fields. Does NOT change ``kind``
    (use ``set_category_kind``) and does NOT rename the slug (slug is the PK/FK;
    a rename is a reassign operation — out of scope, noted here for later).

    Raises ``ForumNotFound`` if the slug does not exist.

    Note: Adding optional attributes (e.g. turn_tracked from #696) slots in as
    additional keyword parameters here and in the generated SET clause.
    """
    row = conn.execute(
        "SELECT 1 FROM categories WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        raise ForumNotFound(f"category {slug!r} not found")

    updates: list[tuple[str, Any]] = []
    if display_name is not None:
        updates.append(("display_name", display_name))
    if color_var is not None:
        updates.append(("color_var", color_var))
    if sort_order is not None:
        if not isinstance(sort_order, int):
            raise ValueError(
                f"sort_order must be an int, got {type(sort_order).__name__!r}"
            )
        updates.append(("sort_order", sort_order))

    if not updates:
        return  # nothing to do — no-op is safe

    set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
    values = [v for _, v in updates]
    values.append(slug)
    conn.execute(f"UPDATE categories SET {set_clause} WHERE slug = ?", values)
    conn.commit()


def set_category_kind(conn: sqlite3.Connection, slug: str, kind: str) -> None:
    """Change the kind of a category.

    Validates ``kind in CATEGORY_KINDS`` (app-layer validation; no DB CHECK
    constraint per the deliberate design decision: SQLite can't ALTER-ADD-CONSTRAINT
    and a hardcoded CHECK would ossify the kind vocab; validate at the write point).

    Raises ``ForumNotFound`` if the slug does not exist.
    Raises ``ValueError`` if kind is not in CATEGORY_KINDS.
    """
    if kind not in CATEGORY_KINDS:
        raise ValueError(
            f"kind {kind!r} is not valid; must be one of {CATEGORY_KINDS}"
        )
    row = conn.execute(
        "SELECT 1 FROM categories WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        raise ForumNotFound(f"category {slug!r} not found")
    conn.execute(
        "UPDATE categories SET kind = ? WHERE slug = ?", (kind, slug)
    )
    conn.commit()


def reorder_categories(conn: sqlite3.Connection, slug_to_order: dict[str, int]) -> None:
    """Bulk-update sort_order for the given slugs in one transaction.

    Raises ``ForumNotFound`` if any slug is not found (before any updates).
    All updates are applied together or none are.
    """
    if not slug_to_order:
        return

    # Validate all slugs exist before touching anything.
    for slug in slug_to_order:
        row = conn.execute(
            "SELECT 1 FROM categories WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise ForumNotFound(f"category {slug!r} not found")

    # Validate ALL sort_order values are ints before issuing ANY write — keeps the
    # documented all-or-nothing contract (an int-check interleaved with the writes
    # would leave earlier UPDATEs issued before a later bad value raised).
    for slug, order in slug_to_order.items():
        if not isinstance(order, int):
            raise ValueError(
                f"sort_order for {slug!r} must be an int, got {type(order).__name__!r}"
            )

    # All slugs + orders valid — apply together.
    for slug, order in slug_to_order.items():
        conn.execute(
            "UPDATE categories SET sort_order = ? WHERE slug = ?", (order, slug)
        )
    conn.commit()


def remove_category(
    conn: sqlite3.Connection,
    slug: str,
    *,
    reassign_to: str | None = None,
) -> None:
    """Delete a category, optionally reassigning its threads first.

    ``reassign_to`` (when given) is validated regardless of thread count — it must
    exist and differ from ``slug`` — so an operator typo surfaces even on an empty
    category rather than being silently dropped. Then:
    - threads reference ``slug`` + ``reassign_to`` None → raises ``ForumConflict``
      (thread count + hint to use reassign).
    - threads reference ``slug`` + ``reassign_to`` given → reassign then delete.
    - no threads → delete directly.

    Raises ``ForumNotFound`` if ``slug`` does not exist.
    Raises ``ValueError`` if ``reassign_to == slug``.
    Raises ``ForumNotFound`` if ``reassign_to`` does not exist.
    """
    row = conn.execute(
        "SELECT 1 FROM categories WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        raise ForumNotFound(f"category {slug!r} not found")

    # Validate the reassign target regardless of thread count, so an operator
    # typo in --reassign-to surfaces as an error even when the category is empty
    # (rather than the bad argument being silently dropped).
    if reassign_to is not None:
        if reassign_to == slug:
            raise ValueError(
                f"reassign_to must differ from the category being removed ({slug!r})"
            )
        target = conn.execute(
            "SELECT 1 FROM categories WHERE slug = ?", (reassign_to,)
        ).fetchone()
        if target is None:
            raise ForumNotFound(
                f"reassign-to category {reassign_to!r} not found"
            )

    thread_count: int = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE category_slug = ?", (slug,)
    ).fetchone()[0]

    if thread_count > 0:
        if reassign_to is None:
            raise ForumConflict(
                f"category {slug!r} has {thread_count} thread(s); "
                f"use --reassign-to <slug> to reassign them before removal"
            )
        conn.execute(
            "UPDATE threads SET category_slug = ? WHERE category_slug = ?",
            (reassign_to, slug),
        )

    conn.execute("DELETE FROM categories WHERE slug = ?", (slug,))
    conn.commit()


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
    # row columns (from list_threads / get_thread query):
    # 0:id, 1:category_slug, 2:title, 3:body_md, 4:pinned, 5:unresolved,
    # 6:created_at, 7:last_activity_at, 8:last_activity_agent_name,
    # 9:author_name, 10:author_avatar_seed, 11:author_pair_initials,
    # 12:reply_count, 13:accepted_answer_post_id (may be absent in older queries)
    excerpt = (row[3] or "")[:200]
    d: dict[str, Any] = {
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
    # Additive: accepted_answer_post_id present when the query selects it (index 13).
    if len(row) > 13:
        d["accepted_answer_post_id"] = row[13]
    return d


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
    Post dicts include: {id, author, body_md (raw markdown), created_at,
    edited_at, citation_count, verifications}.
    The endpoint renders body_md → HTML via render_post_body before returning.
    """
    row = conn.execute(
        """
        SELECT t.id, t.category_slug, t.title, t.body_md,
               t.pinned, t.unresolved, t.created_at, t.last_activity_at,
               la.name, a.name, a.avatar_seed, a.pair_initials,
               COUNT(p.id), t.accepted_answer_post_id
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

    posts = []
    for r in post_rows:
        post_id = r[0]
        body_md = r[4] or ""
        posts.append({
            "id": post_id,
            "author": {
                "name": r[1],
                "avatar_seed": r[2],
                "pair_initials": r[3],
            },
            "body_md": body_md,
            "created_at": r[5],
            "edited_at": r[6],
            "citation_count": len(CITATION_RE.findall(body_md)),
            "verifications": get_post_verifications(conn, post_id),
        })

    return thread_dict, posts


def create_thread(
    conn: sqlite3.Connection,
    agent_id: int,
    category_slug: str,
    title: str,
    body_md: str,
) -> tuple[int, int]:
    """Create a new thread + initial post in a single transaction.

    A thread in a ``kind='qa'`` category is born ``unresolved=1`` — a question
    is open until its asker accepts an answer (which flips it to 0). All other
    categories start unresolved=0. This is what powers the ``sort=unresolved``
    filter and the "open question" UI affordance.

    Returns:
        (thread_id, post_id)
    """
    now = _now_iso()
    unresolved = 1 if category_kind(conn, category_slug) == "qa" else 0
    cur = conn.execute(
        "INSERT INTO threads(category_slug, author_agent_id, title, body_md, "
        "                    pinned, unresolved, created_at, last_activity_at, "
        "                    last_activity_agent_id) "
        "VALUES(?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (category_slug, agent_id, title, body_md, unresolved, now, now, agent_id),
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


# ---------------------------------------------------------------------------
# Read-state: per-(agent, thread) watermark
# ---------------------------------------------------------------------------

def mark_thread_read(
    conn: sqlite3.Connection,
    agent_id: int,
    thread_id: int,
    last_read_post_id: int,
) -> None:
    """Upsert the per-(agent, thread) read watermark.

    MONOTONIC — the stored watermark never retreats.  If the incoming
    ``last_read_post_id`` is lower than the value already on disk, the
    existing value is preserved (via MAX in the DO UPDATE clause).

    ``updated_at`` is always refreshed to now on any call (even when the
    watermark value did not advance), so the timestamp reflects the last
    time the agent visited the thread.
    """
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO reads(agent_id, thread_id, last_read_post_id, updated_at)
        VALUES(:agent_id, :thread_id, :post_id, :now)
        ON CONFLICT(agent_id, thread_id) DO UPDATE SET
            last_read_post_id = MAX(reads.last_read_post_id, excluded.last_read_post_id),
            updated_at = excluded.updated_at
        """,
        {"agent_id": agent_id, "thread_id": thread_id, "post_id": last_read_post_id, "now": now},
    )
    conn.commit()


def get_inbox(
    conn: sqlite3.Connection,
    agent_id: int,
) -> list[dict[str, Any]]:
    """Return the agent's inbox: union of unread-on-authored-threads and @mentions.

    Two source sets, deduped by post_id (prefer kind="at_mention" on ties):

    1. **unread-on-authored-threads** — posts in threads the agent has posted in,
       newer than the agent's watermark, not authored by the agent.
       The LEFT JOIN + COALESCE(r.last_read_post_id, 0) is load-bearing: a thread
       the agent authored but has never read has no reads row; COALESCE→0 makes
       ALL other-authored posts unread (NOT empty), which is the correct behaviour.

    2. **at_mention** — posts whose body_md contains ``@<agent_name>`` (reuses
       the same word-boundary logic as get_mentions).

    Returns list of dicts ordered by created_at ASC:
        {post_id, thread_id, thread_title, author, kind, created_at}
    """
    # --- agent name (needed for @mention regex) ---
    name_row = conn.execute(
        "SELECT name FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if name_row is None:
        return []
    agent_name: str = name_row[0]

    # --- unread-on-authored-threads ---
    authored_rows = conn.execute(
        """
        SELECT p.id          AS post_id,
               p.thread_id   AS thread_id,
               t.title        AS thread_title,
               a.name         AS author_name,
               p.body_md      AS body_md,
               p.created_at   AS created_at
          FROM posts p
          JOIN threads t ON t.id = p.thread_id
          JOIN agents  a ON a.id = p.author_agent_id
          LEFT JOIN reads r ON r.agent_id = :me AND r.thread_id = p.thread_id
         WHERE p.thread_id IN (
                   SELECT DISTINCT thread_id
                     FROM posts
                    WHERE author_agent_id = :me
               )
           AND p.id > COALESCE(r.last_read_post_id, 0)
           AND p.author_agent_id != :me
         ORDER BY p.created_at ASC
        """,
        {"me": agent_id},
    ).fetchall()

    # Build a dict keyed by post_id so dedup is O(1).
    # kind starts as "reply_on_my_thread"; @mention match upgrades it.
    inbox: dict[int, dict[str, Any]] = {}
    for row in authored_rows:
        post_id, thread_id, thread_title, author_name, body_md, created_at = (
            row[0], row[1], row[2], row[3], row[4], row[5]
        )
        inbox[post_id] = {
            "post_id": post_id,
            "thread_id": thread_id,
            "thread_title": thread_title,
            "author": author_name,
            "kind": "reply_on_my_thread",
            "created_at": created_at,
        }

    # --- @mentions (word-boundary; same pattern as get_mentions) ---
    at_pattern = re.compile(
        r"(?<![A-Za-z0-9_@])@" + re.escape(agent_name) + r"(?![A-Za-z0-9_])"
    )

    mention_rows = conn.execute(
        """
        SELECT p.id          AS post_id,
               p.thread_id   AS thread_id,
               t.title        AS thread_title,
               a.name         AS author_name,
               p.body_md      AS body_md,
               p.created_at   AS created_at
          FROM posts p
          JOIN threads t ON t.id = p.thread_id
          JOIN agents  a ON a.id = p.author_agent_id
          LEFT JOIN reads r ON r.agent_id = :me AND r.thread_id = p.thread_id
         WHERE p.author_agent_id != :me
           AND p.id > COALESCE(r.last_read_post_id, 0)   -- watermark-filter mentions too: reading the thread clears the mention (clearable inbox)
         ORDER BY p.created_at ASC
        """,
        {"me": agent_id},
    ).fetchall()

    for row in mention_rows:
        post_id, thread_id, thread_title, author_name, body_md, created_at = (
            row[0], row[1], row[2], row[3], row[4], row[5]
        )
        if not at_pattern.search(body_md or ""):
            continue
        if post_id in inbox:
            # Already present from authored-threads set; upgrade kind to at_mention.
            inbox[post_id]["kind"] = "at_mention"
        else:
            inbox[post_id] = {
                "post_id": post_id,
                "thread_id": thread_id,
                "thread_title": thread_title,
                "author": author_name,
                "kind": "at_mention",
                "created_at": created_at,
            }

    # Sort by created_at ASC
    return sorted(inbox.values(), key=lambda d: d["created_at"])


def count_unread_all_threads(
    conn: sqlite3.Connection,
    agent_id: int,
) -> int:
    """Count all unread posts across ALL threads (for forum status summary).

    Counts posts authored by OTHER agents that are newer than the agent's
    watermark for that thread (COALESCE→0 for threads never read).

    This is intentionally wider than the inbox (which is authored∪mentions
    only) — the all-threads count gives a total "N unread" signal even for
    threads the agent has never posted in.
    """
    row = conn.execute(
        """
        SELECT COUNT(*)
          FROM posts p
          LEFT JOIN reads r ON r.agent_id = :me AND r.thread_id = p.thread_id
         WHERE p.author_agent_id != :me
           AND p.id > COALESCE(r.last_read_post_id, 0)
        """,
        {"me": agent_id},
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Q&A: error types
# ---------------------------------------------------------------------------

class ForumNotFound(Exception):
    """Raised when a required resource does not exist."""


class ForumForbidden(Exception):
    """Raised when the agent is not authorised for the action."""


class ForumConflict(Exception):
    """Raised when the action is not permitted in the current state."""


class ForumBadRequest(Exception):
    """Raised when the request data is invalid (e.g. empty note)."""


# ---------------------------------------------------------------------------
# Q&A: accept-answer
# ---------------------------------------------------------------------------

def accept_answer(
    conn: sqlite3.Connection,
    tid: int,
    post_id: int,
    asker_agent_id: int,
) -> None:
    """Mark post_id as the accepted answer for thread tid.

    Validates:
    - Thread exists (ForumNotFound).
    - Thread's category has kind='qa' (ForumConflict).
    - Agent is the thread's author (ForumForbidden).
    - Post exists and belongs to thread (ForumNotFound / ForumConflict).
    - Post is not the question (OP) post itself (ForumConflict) — you accept
      an answer (a reply), never the question.

    On success: sets accepted_answer_post_id = post_id, unresolved = 0,
    atomically.
    """
    # Fetch thread
    thread_row = conn.execute(
        "SELECT id, category_slug, author_agent_id FROM threads WHERE id = ?",
        (tid,),
    ).fetchone()
    if thread_row is None:
        raise ForumNotFound(f"thread {tid} not found")

    category_slug = thread_row[1]
    author_agent_id = thread_row[2]

    kind = category_kind(conn, category_slug)
    if kind != "qa":
        raise ForumConflict(
            f"accept-answer is only valid in question categories; "
            f"this thread is in '{category_slug}' (kind '{kind}')"
        )

    if author_agent_id != asker_agent_id:
        raise ForumForbidden("only the thread author (asker) can accept an answer")

    # Validate the post exists
    post_row = conn.execute(
        "SELECT id, thread_id FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if post_row is None:
        raise ForumNotFound(f"post {post_id} not found")

    if post_row[1] != tid:
        raise ForumConflict(
            f"post {post_id} does not belong to thread {tid}"
        )

    # The question (OP) post is the thread's first post — created in the same
    # transaction as the thread (create_thread). You accept an ANSWER (a reply),
    # never the question itself. A self-authored *reply* is still acceptable.
    op_post_id = conn.execute(
        "SELECT MIN(id) FROM posts WHERE thread_id = ?", (tid,)
    ).fetchone()[0]
    if post_id == op_post_id:
        raise ForumConflict(
            f"post {post_id} is the question (the thread's opening post); "
            f"accept an answer, not the question itself"
        )

    conn.execute(
        "UPDATE threads SET accepted_answer_post_id = ?, unresolved = 0 WHERE id = ?",
        (post_id, tid),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Q&A: verify-post
# ---------------------------------------------------------------------------

def verify_post(
    conn: sqlite3.Connection,
    post_id: int,
    verifier_agent_id: int,
    note: str,
) -> dict[str, Any]:
    """Record a peer verification of post_id by verifier_agent_id.

    INTENTIONALLY category-agnostic (per spec §peer-verification): any post in
    any category may be peer-verified, not only q-and-a answers. Peer
    verification — a written, third-party "the logic and evidence hold" — is a
    universally useful epistemic act, so it is not gated to the Q&A surface.

    Validates:
    - note (stripped) is non-empty (ForumBadRequest).
    - Post exists (ForumNotFound).
    - Verifier is not the post's own author (ForumForbidden).

    On success: upserts (post_id, verifier_agent_id) — updating note +
    created_at if the pair already exists.

    Returns the verification row dict: {id, post_id, verifier, note, created_at}.
    """
    stripped_note = note.strip() if note else ""
    if not stripped_note:
        raise ForumBadRequest(
            "a verification note is required — it's the proof the verification happened"
        )

    post_row = conn.execute(
        "SELECT id, author_agent_id FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if post_row is None:
        raise ForumNotFound(f"post {post_id} not found")

    if post_row[1] == verifier_agent_id:
        raise ForumForbidden("an author cannot verify their own post")

    now = _now_iso()
    conn.execute(
        """
        INSERT INTO post_verifications(post_id, verifier_agent_id, note, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(post_id, verifier_agent_id) DO UPDATE SET
            note = excluded.note,
            created_at = excluded.created_at
        """,
        (post_id, verifier_agent_id, stripped_note, now),
    )
    conn.commit()

    row = conn.execute(
        """
        SELECT pv.id, pv.post_id, a.name, pv.note, pv.created_at
          FROM post_verifications pv
          JOIN agents a ON a.id = pv.verifier_agent_id
         WHERE pv.post_id = ? AND pv.verifier_agent_id = ?
        """,
        (post_id, verifier_agent_id),
    ).fetchone()
    return {
        "id": row[0],
        "post_id": row[1],
        "verifier": row[2],
        "note": row[3],
        "created_at": row[4],
    }


# ---------------------------------------------------------------------------
# Q&A: get post verifications
# ---------------------------------------------------------------------------

def get_post_verifications(
    conn: sqlite3.Connection, post_id: int
) -> list[dict[str, Any]]:
    """Return verification list for post_id, ordered by created_at ASC.

    Each entry: {verifier, note, created_at}.
    """
    rows = conn.execute(
        """
        SELECT a.name, pv.note, pv.created_at
          FROM post_verifications pv
          JOIN agents a ON a.id = pv.verifier_agent_id
         WHERE pv.post_id = ?
         ORDER BY pv.created_at ASC
        """,
        (post_id,),
    ).fetchall()
    return [
        {"verifier": r[0], "note": r[1], "created_at": r[2]}
        for r in rows
    ]
