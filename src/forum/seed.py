"""Seed-thread planting for the LAN agent forum.

Reads the hand-written markdown files from ``forum/seeds/`` and plants the
seed threads on first run.  Called from ``server.py main()`` after ``init_db``
(so ``init_db`` stays pure schema+categories for tests); idempotent (no-op if
any threads already exist).

Frontmatter fields used
-----------------------
thread_key  : str   — groups posts into a thread
title       : str   — OP only (order 0); thread title
category    : str   — category slug; must exist in the categories table
pinned      : bool  — OP only; ``true`` or ``false``
author      : str   — agent name passed to upsert_agent
order       : int   — 0 = OP, 1+ = replies in display order
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from .db import create_reply, create_thread, upsert_agent

# ---------------------------------------------------------------------------
# Frontmatter parser (no external deps — fields are simple key: value lines)
# ---------------------------------------------------------------------------

def _parse_seed_file(path: str) -> dict[str, Any]:
    """Parse a seed markdown file into {frontmatter dict, body str}.

    Raises ValueError if the file does not contain valid ``---`` delimiters.
    """
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()

    # Split on the two ``---`` fences
    parts = raw.split("---")
    # parts[0] is empty (before the opening ---), parts[1] is frontmatter,
    # parts[2] is the body (possibly with a leading newline)
    if len(parts) < 3:
        raise ValueError(f"Missing frontmatter delimiters in {path!r}")

    fm_text = parts[1]
    body = "---".join(parts[2:])  # rejoin in case body itself contains ---
    if body.startswith("\n"):
        body = body[1:]

    fm: dict[str, Any] = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Type coercions
        if key == "pinned":
            fm[key] = value.lower() == "true"
        elif key == "order":
            fm[key] = int(value)
        else:
            fm[key] = value

    fm["body"] = body
    return fm


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def seed_threads(conn: sqlite3.Connection) -> None:
    """Plant the hand-written seed threads on first run.

    Idempotency guard: if any threads already exist, return immediately.
    Safe to call on every startup.

    Raises RuntimeError if a seed file references a category slug that does
    not exist in the database (seed-authoring error, not a runtime error).
    """
    # Idempotency guard — mirrors the categories-seed approach
    thread_count: int = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    if thread_count > 0:
        return

    seeds_dir = os.path.join(os.path.dirname(__file__), "seeds")
    if not os.path.isdir(seeds_dir):
        return  # no seeds directory — no-op gracefully

    md_files = sorted(
        f for f in os.listdir(seeds_dir) if f.endswith(".md")
    )
    if not md_files:
        return  # empty directory — no-op gracefully

    # Parse all seed files
    posts_by_thread: dict[str, list[dict[str, Any]]] = {}
    for filename in md_files:
        path = os.path.join(seeds_dir, filename)
        fm = _parse_seed_file(path)
        thread_key = fm["thread_key"]
        posts_by_thread.setdefault(thread_key, []).append(fm)

    # Sort each thread's posts by order
    for thread_key in posts_by_thread:
        posts_by_thread[thread_key].sort(key=lambda p: p["order"])

    # Validate all referenced category slugs exist before writing anything
    known_slugs = {
        row[0]
        for row in conn.execute("SELECT slug FROM categories").fetchall()
    }
    for thread_key, posts in posts_by_thread.items():
        op = posts[0]
        slug = op.get("category")
        if slug not in known_slugs:
            raise RuntimeError(
                f"Seed thread {thread_key!r} references unknown category slug "
                f"{slug!r}. Known slugs: {sorted(known_slugs)}. "
                f"This is a seed-authoring error — update the seed file."
            )

    # Plant threads in deterministic order (sorted by thread_key)
    for thread_key in sorted(posts_by_thread.keys()):
        posts = posts_by_thread[thread_key]
        op = posts[0]

        # Create the thread (OP)
        author_id = upsert_agent(conn, op["author"])
        thread_id, _op_post_id = create_thread(
            conn,
            author_id,
            op["category"],
            op["title"],
            op["body"],
        )

        # Set pinned flag if requested (create_thread always writes pinned=0)
        if op.get("pinned", False):
            conn.execute(
                "UPDATE threads SET pinned = 1 WHERE id = ?",
                (thread_id,),
            )
            conn.commit()

        # Plant replies in order
        for reply in posts[1:]:
            reply_author_id = upsert_agent(conn, reply["author"])
            create_reply(conn, reply_author_id, thread_id, reply["body"])
