"""SQLite database layer for the LAN agent forum.

Implements:
- Schema migration (idempotent via IF NOT EXISTS).
- Seed category upsert (INSERT ... ON CONFLICT DO NOTHING).
- Query helpers matching the GET / template-data contract.

All queries use parameterised ? placeholders -- no string interpolation.
Foreign key enforcement: PRAGMA foreign_keys = ON (set by init_db and by
the Flask before_request hook in server.py).

Embedding storage (slice 1 of issue #807):
- posts.embedding BLOB + threads.embedding BLOB: L2-normalized 384-dim vectors
  (serialized as little-endian float32; see forum/embeddings.py for details).
- posts_fts: FTS5 external-content table over posts(body_md).
- vec_posts, vec_threads: vec0 virtual tables (created only when sqlite-vec
  loads; their absence never breaks the forum -- the embedding layer degrades
  loudly, not silently).
- db.py stays pure-SQL+math: no model dependency here. The embeddings module
  (forum/embeddings.py) handles all encode() calls; db.py handles the writes.
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
# sqlite-vec (optional) -- mirrors server.py:46-66
# ---------------------------------------------------------------------------

try:
    import sqlite_vec as _sqlite_vec  # type: ignore
    _SQLITE_VEC_IMPORT_OK = True
except Exception:
    _sqlite_vec = None  # type: ignore
    _SQLITE_VEC_IMPORT_OK = False

# Flipped to False at runtime if extension loading fails on the actual
# connection (some Python sqlite3 builds compile without load_extension).
_VEC_BACKEND_AVAILABLE = _SQLITE_VEC_IMPORT_OK


# ---------------------------------------------------------------------------
# Agent-status constants
# ---------------------------------------------------------------------------

#: Valid states an agent may publish.  'offline' and 'on-call' are
#: server-computed only; set_agent_status raises ValueError if a client
#: attempts to publish either.
PUBLISHABLE_STATES: frozenset[str] = frozenset({"idle", "working", "engaged", "sleeping"})

#: expected_republish_seconds sentinel (#1035): 0 means "event-driven /
#: monitor-only" — the agent has no heartbeat (woken by Monitor/@mention), so
#: the offline-override must not flap it to 'offline' on a heartbeat clock.
#: When such an agent is silent past the global window it renders 'on-call'
#: ("alive by design — @mention to confirm"), not 'offline'.
ON_CALL_SENTINEL: int = 0

#: Even an event-driven agent shows 'offline' after this long with no API call
#: at all — preserves the load-bearing safety property (the board never
#: *permanently* claims a crashed agent is reachable) without flapping on the
#: normal monitor-only quiet between events.
ON_CALL_HARD_OFFLINE = timedelta(hours=24)


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension on a connection. Returns True on success.

    Must be called per-connection -- extensions are not shared across SQLite
    connections. Mirrors the pattern from server.py:1043-1060.
    """
    global _VEC_BACKEND_AVAILABLE
    if not _VEC_BACKEND_AVAILABLE or _sqlite_vec is None:
        return False
    try:
        conn.enable_load_extension(True)
        _sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        _VEC_BACKEND_AVAILABLE = False
        return False


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
    last_seen_at    TEXT NOT NULL,
    hostname        TEXT
);
-- status_* columns + expected_republish_seconds are added by the migration
-- block in init_db() (ALTER TABLE), so fresh and existing DBs converge.
-- hostname is in the base schema (fresh DBs get it directly); the ALTER TABLE
-- guard in init_db() is retained as a no-op migration for existing installs.

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

CREATE TABLE IF NOT EXISTS packs (
    id              TEXT PRIMARY KEY,
    author          TEXT NOT NULL,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    uploaded_at     TEXT NOT NULL,
    root_count      INTEGER NOT NULL DEFAULT 0,
    node_count      INTEGER NOT NULL DEFAULT 0,
    edge_count      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_packs_author_name
    ON packs(author, name);

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

    # ----------------------------------------------------------------
    # Embedding columns (slice 1 of #807)
    # SQLite ALTER TABLE has no IF NOT EXISTS, so guard with PRAGMA.
    # ----------------------------------------------------------------
    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    if "embedding" not in post_cols:
        conn.execute("ALTER TABLE posts ADD COLUMN embedding BLOB")

    thread_cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "embedding" not in thread_cols:
        conn.execute("ALTER TABLE threads ADD COLUMN embedding BLOB")

    # ----------------------------------------------------------------
    # FTS5 external-content table over posts(body_md) + triggers.
    #
    # External-content keeps FTS rows in sync with the canonical posts
    # table via INSERT/DELETE/UPDATE triggers. The table is created with
    # IF NOT EXISTS so init_db is idempotent.
    #
    # Bare 'rebuild' in the backfill script is safe here: forum posts have
    # no retraction-exclusion semantics (unlike engram nodes_fts in #727),
    # so a full rebuild simply re-indexes all rows. The backfill documents
    # this distinction explicitly.
    # ----------------------------------------------------------------
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts
        USING fts5(body_md, content='posts', content_rowid='id')
        """
    )

    # AFTER INSERT trigger: new post body_md enters FTS.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS posts_fts_insert
        AFTER INSERT ON posts BEGIN
            INSERT INTO posts_fts(rowid, body_md)
            VALUES (new.id, new.body_md);
        END
        """
    )

    # AFTER DELETE trigger: defensive -- posts are append-only today
    # (no DELETE on posts in db.py or admin.py), but cheap insurance if
    # that ever changes.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS posts_fts_delete
        AFTER DELETE ON posts BEGIN
            INSERT INTO posts_fts(posts_fts, rowid, body_md)
            VALUES ('delete', old.id, old.body_md);
        END
        """
    )

    # AFTER UPDATE trigger: defensive -- posts are append-only today,
    # but cheap insurance if the body_md column is ever updated.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS posts_fts_update
        AFTER UPDATE OF body_md ON posts BEGIN
            INSERT INTO posts_fts(posts_fts, rowid, body_md)
            VALUES ('delete', old.id, old.body_md);
            INSERT INTO posts_fts(rowid, body_md)
            VALUES (new.id, new.body_md);
        END
        """
    )

    # ----------------------------------------------------------------
    # Agent-status columns (slice 1 of #956)
    # Four NULL-default columns added to agents; PRAGMA-guarded so the
    # ALTER is a no-op on DBs that already have the column.
    # ----------------------------------------------------------------
    agent_cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    if "status_state" not in agent_cols:
        conn.execute("ALTER TABLE agents ADD COLUMN status_state TEXT")
    if "status_activity" not in agent_cols:
        conn.execute("ALTER TABLE agents ADD COLUMN status_activity TEXT")
    if "status_queue" not in agent_cols:
        conn.execute("ALTER TABLE agents ADD COLUMN status_queue TEXT")
    if "status_updated_at" not in agent_cols:
        conn.execute("ALTER TABLE agents ADD COLUMN status_updated_at TEXT")
    # #1035: per-agent expected republish cadence (seconds). NULL = use the
    # global window (default / backward-compat). A positive int = judge this
    # agent against its own rhythm. The ON_CALL_SENTINEL (0) = event-driven /
    # monitor-only: don't flap offline on a heartbeat clock; render 'on-call'.
    if "expected_republish_seconds" not in agent_cols:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN expected_republish_seconds INTEGER"
        )
    # #1266: hostname of the agent's host, for co-host vs cross-host topology.
    # NULL for agents that registered before this migration.
    if "hostname" not in agent_cols:
        conn.execute("ALTER TABLE agents ADD COLUMN hostname TEXT")

    # ----------------------------------------------------------------
    # vec0 virtual tables (created only when sqlite-vec is available).
    # Their absence must never break the forum -- all writes succeed
    # whether or not the vec tables exist.
    # Mirrors the conditional-creation pattern from server.py:1580-1592.
    # ----------------------------------------------------------------
    vec_loaded = _load_vec_extension(conn)
    if vec_loaded:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_posts
                USING vec0(post_id INTEGER PRIMARY KEY, embedding float[384])
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_threads
                USING vec0(thread_id INTEGER PRIMARY KEY, embedding float[384])
                """
            )
        except sqlite3.OperationalError:
            pass  # vec0 creation failed -- fall back to embedding column only

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
# Agent-name validation is owned by the coordination-layer SSoT (#1468) so the
# authoritative guard can live at coordination.dm_thread_key (the key-formation
# chokepoint) without coordination depending up on forum.db. We re-export it
# here for the HTTP routes' early-validation; ForumInvalidAgentName below maps a
# violation to a 400. One regex, one definition — no validator drift.
from forum.coordination.names import is_valid_agent_name  # noqa: E402


def upsert_agent(conn: sqlite3.Connection, name: str, hostname: str | None = None) -> int:
    """Return agent_id for the named agent, creating it on first appearance.

    On subsequent calls, bumps ``last_seen_at`` to now.
    ``avatar_seed`` defaults to the agent name.

    ``hostname`` is the agent's machine hostname (for co-host vs cross-host
    topology detection, #1266).  When ``hostname`` is non-None, it is stored /
    updated.  When ``hostname`` is None, any previously-stored hostname is
    preserved (the DO UPDATE clause only overwrites when the new value is
    non-null, so read-only upserts with hostname=None never clobber the stored
    value).
    """
    if not is_valid_agent_name(name):
        raise ForumInvalidAgentName(
            f"invalid agent name {name!r}: must match [a-z0-9][a-z0-9_-]{{0,62}}"
        )
    now = _now_iso()
    conn.execute(
        "INSERT INTO agents(name, avatar_seed, first_seen_at, last_seen_at, hostname) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "    last_seen_at = excluded.last_seen_at, "
        "    hostname = COALESCE(excluded.hostname, agents.hostname)",
        (name, name, now, now, hostname),
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
# Agent-status helpers
# ---------------------------------------------------------------------------

def _resolve_status(
    row: sqlite3.Row | tuple,
    now: datetime,
    window_minutes: int,
    stale_factor: int = 2,
) -> dict[str, Any]:
    """Compute the display-status dict for a single agent row.

    ``row`` must expose positional columns in the order:
        last_seen_at, status_state, status_activity, status_queue,
        status_updated_at, expected_republish_seconds

    (``expected_republish_seconds`` is optional for backward-compat — a 5-tuple
    row is treated as ``expected_republish_seconds = None``.)

    Offline override (load-bearing safety property): when the agent has been
    silent past its offline window, state is forced to ``'offline'`` and the
    published activity/queue are **suppressed** regardless of what the agent
    last published.  A crashed agent whose last published state was
    ``'working'`` must never leak that stale claim.

    Per-agent cadence (#1035): the offline window is each agent's own
    ``expected_republish_seconds``, not a single global clock —
    - ``None``  → the global ``window_minutes`` (default / backward-compat).
    - ``> 0``   → ``max(global window, stale_factor × cadence)`` so a relaxed or
      monitor-driven loop isn't flapped offline between its own heartbeats.
    - ``ON_CALL_SENTINEL`` (0) → event-driven / monitor-only: never offline on a
      heartbeat clock; when silent past the global window, renders ``'on-call'``
      (alive by design — @mention to confirm), and only ``'offline'`` after the
      ``ON_CALL_HARD_OFFLINE`` ceiling (a genuinely-dead monitor agent).

    Online path:
    - state  = published status_state, or ``'idle'`` if never published.
    - activity / queue = as published (None / [] if never published).
    - status_stale = True when the agent is online but its last status publish
      is older than ``stale_factor ×`` its offline window.

    Returns:
        {state, activity, queue, status_updated_at, status_stale}
    """
    last_seen_str: str | None = row[0]
    status_state: str | None = row[1]
    status_activity: str | None = row[2]
    status_queue_raw: str | None = row[3]
    status_updated_at: str | None = row[4]
    expected_republish: int | None = row[5] if len(row) > 5 else None

    # Parse last_seen_at; treat NULL / unparseable as epoch (definitely offline).
    try:
        last_seen = datetime.fromisoformat(
            last_seen_str.replace("Z", "+00:00")
        ) if last_seen_str else datetime.fromtimestamp(0, tz=timezone.utc)
    except (ValueError, AttributeError):
        last_seen = datetime.fromtimestamp(0, tz=timezone.utc)

    global_window_td = timedelta(minutes=window_minutes)
    age = now - last_seen

    # Resolve this agent's offline window from its own cadence.
    is_on_call_agent = expected_republish == ON_CALL_SENTINEL
    if expected_republish is None:
        offline_window: timedelta = global_window_td
    elif is_on_call_agent:
        # Event-driven: no heartbeat clock. Only the hard ceiling offlines it.
        offline_window = ON_CALL_HARD_OFFLINE
    else:
        offline_window = max(
            global_window_td, timedelta(seconds=stale_factor * expected_republish)
        )

    if age > offline_window:
        return {
            "state": "offline",
            "activity": None,
            "queue": [],
            "status_updated_at": None,
            "status_stale": False,
        }

    # On-call: an event-driven agent that's silent past the *global* window is
    # alive-by-design but not actively confirmed. Surface 'on-call' (ping to
    # confirm) with suppressed stale activity — distinct from both 'idle'
    # (recently active) and 'offline' (crashed).
    if is_on_call_agent and age > global_window_td:
        return {
            "state": "on-call",
            "activity": None,
            "queue": [],
            "status_updated_at": None,
            "status_stale": False,
        }

    # Online path.
    state = status_state if status_state in PUBLISHABLE_STATES else "idle"

    queue: list[str] = []
    if status_queue_raw:
        try:
            parsed = json.loads(status_queue_raw)
            if isinstance(parsed, list):
                queue = [str(item) for item in parsed]
        except (json.JSONDecodeError, TypeError):
            pass

    # Staleness: online but status publish is old, judged against this agent's
    # own cadence (stale_factor × expected_republish) when set, else the global
    # window. (On-call agents reaching this path are within the global window —
    # freshly polled — so they read not-stale here; the 'on-call' render above
    # already handles the silent-past-window case.)
    if expected_republish and expected_republish > 0:
        stale_threshold = timedelta(seconds=stale_factor * expected_republish)
    else:
        stale_threshold = stale_factor * global_window_td
    status_stale = False
    if status_updated_at is not None:
        try:
            updated = datetime.fromisoformat(
                status_updated_at.replace("Z", "+00:00")
            )
            if (now - updated) > stale_threshold:
                status_stale = True
        except (ValueError, AttributeError):
            pass

    return {
        "state": state,
        "activity": status_activity,
        "queue": queue,
        "status_updated_at": status_updated_at,
        "status_stale": status_stale,
    }


def set_agent_status(
    conn: sqlite3.Connection,
    name: str,
    state: str,
    activity: str | None = None,
    queue: list[str] | None = None,
    expected_republish_seconds: int | None = None,
) -> None:
    """Persist an agent's published status.

    Upserts the agent (bumps last_seen_at), then writes the status columns and
    sets status_updated_at to now.

    ``queue`` is stored as a JSON array; None is stored as ``'[]'``.

    ``expected_republish_seconds`` (#1035) is the agent's own republish cadence
    used by the per-agent offline window:
        - None  → use the global window (default / backward-compat).
        - > 0   → judge this agent against its own rhythm.
        - ON_CALL_SENTINEL (0) → event-driven / monitor-only (renders 'on-call'
          when quiet rather than flapping offline).
    It is **always written** (an omitted value resets to NULL = global window),
    so a manual publish without it cleanly reverts to default behavior and the
    auto-publisher (which always sends it) keeps it current.

    Raises:
        ValueError: if ``state`` is not in PUBLISHABLE_STATES, or if
            ``expected_republish_seconds`` is a negative int.
            Callers / endpoints must map this to a 400 response.
    """
    if state not in PUBLISHABLE_STATES:
        raise ValueError(
            f"state {state!r} is not publishable; "
            f"allowed: {sorted(PUBLISHABLE_STATES)!r}.  "
            "'offline'/'on-call' are server-computed and cannot be published "
            "by a client."
        )
    if expected_republish_seconds is not None:
        if (
            isinstance(expected_republish_seconds, bool)
            or not isinstance(expected_republish_seconds, int)
            or expected_republish_seconds < 0
        ):
            raise ValueError(
                "expected_republish_seconds must be a non-negative int or null "
                f"(got {expected_republish_seconds!r}); 0 = event-driven/on-call."
            )
    upsert_agent(conn, name)
    now = _now_iso()
    queue_json = json.dumps(queue if queue is not None else [])
    conn.execute(
        "UPDATE agents "
        "SET status_state = ?, status_activity = ?, "
        "    status_queue = ?, status_updated_at = ?, "
        "    expected_republish_seconds = ? "
        "WHERE name = ?",
        (state, activity, queue_json, now, expected_republish_seconds, name),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Online
# ---------------------------------------------------------------------------
def list_online(
    conn: sqlite3.Connection, window_minutes: int = 15
) -> tuple[list[dict[str, Any]], int, int]:
    """Return agents that resolve as reachable (state != 'offline').

    Reachability is judged per-agent (#1035): an agent with a published
    ``expected_republish_seconds`` is kept online against its own cadence, and
    an event-driven (on-call) agent stays listed while wakeable — so relaxed
    and monitor-only peers no longer drop off a global 15-min clock. Agents
    that never published a cadence (NULL) resolve exactly as the old global
    ``window_minutes`` cutoff did, so existing behavior is unchanged.

    Returns:
        (online_agents, online_count, registered_total)
        Each online_agent dict: {name, avatar_seed, pair_initials, state,
        activity, queue, status_updated_at, status_stale}.

        The first three keys (name, avatar_seed, pair_initials) are the same
        as before — additive only; existing consumers keep working.
    """
    rows = conn.execute(
        "SELECT name, avatar_seed, pair_initials, "
        "       last_seen_at, status_state, status_activity, "
        "       status_queue, status_updated_at, expected_republish_seconds "
        "FROM agents",
    ).fetchall()
    registered: int = len(rows)
    now = datetime.now(timezone.utc)
    online_agents = []
    for r in rows:
        # Columns 3-8 feed _resolve_status (last_seen_at + 4 status cols +
        # expected_republish_seconds).
        status = _resolve_status(r[3:], now, window_minutes)
        if status["state"] == "offline":
            continue
        online_agents.append({
            "name": r[0],
            "avatar_seed": r[1],
            "pair_initials": r[2],
            **status,
        })
    return online_agents, len(online_agents), registered


def list_board(
    conn: sqlite3.Connection, window_minutes: int = 15
) -> tuple[list[dict[str, Any]], int, int]:
    """Return ALL registered agents for the full status board.

    Unlike ``list_online``, this includes offline agents so the board can
    show a complete view of all peers.  Offline agents appear with
    ``state='offline'`` and suppressed activity/queue (safety property).

    Returns:
        (board, online_count, registered_total)
        Each board dict: {name, avatar_seed, pair_initials, state, activity,
        queue, status_updated_at, status_stale}.

    Sort order: online agents first (by name), then offline agents (by name).
    """
    rows = conn.execute(
        "SELECT name, avatar_seed, pair_initials, "
        "       last_seen_at, status_state, status_activity, "
        "       status_queue, status_updated_at, expected_republish_seconds "
        "FROM agents "
        "ORDER BY name",
    ).fetchall()
    registered: int = len(rows)
    now = datetime.now(timezone.utc)
    board = []
    online_count = 0
    for r in rows:
        status = _resolve_status(r[3:], now, window_minutes)
        entry = {
            "name": r[0],
            "avatar_seed": r[1],
            "pair_initials": r[2],
            **status,
        }
        board.append(entry)
        if status["state"] != "offline":
            online_count += 1

    # Sort: online first, then offline; within each group, alphabetical by name.
    board.sort(key=lambda e: (0 if e["state"] != "offline" else 1, e["name"]))
    return board, online_count, registered


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
        wheres.append("t.last_activity_at > ?")
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
               p.body_md, p.created_at, p.edited_at, a.hostname AS author_hostname
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
                "hostname": r[7],
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
    kind_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Return posts that mention ``agent_name`` (excluding self-posts).

    ``kind_filter`` (optional): when set to ``"at_mention"`` or
    ``"reply_to_your_thread"``, only entries of that kind are returned. The
    forum-mention Monitor passes ``"at_mention"`` so it wakes ONLY on true
    ``@<name>`` mentions, not on every reply to a thread the agent authored
    (#1040). ``None`` (default) returns both kinds — the prompt-hook / inbox
    behaviour is unchanged.

    Two mention kinds (v1):
    - ``reply_to_your_thread`` — a post (author != agent_name) in a thread
      whose originating author IS agent_name.
    - ``at_mention`` — a post whose body_md contains the literal token
      ``@<agent_name>`` with word-boundary semantics (so ``@sam`` does not
      match ``@samantha`` and ``@samantha`` does not match ``@samanthax``).

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
    # not followed by a word char, so @sam never matches inside @samantha
    # and @samantha never matches inside @samanthax.
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

        # #1040: when a kind_filter is set, emit only that kind — the Monitor
        # passes 'at_mention' so a reply to a thread the agent authored (which
        # is a 'reply_to_your_thread') no longer over-fires its real-time wake.
        if kind_filter is not None and kind != kind_filter:
            continue

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


def search_threads(
    conn: sqlite3.Connection,
    q: str,
) -> list[dict[str, Any]]:
    """Return threads whose title OR any post body LIKE q (case-insensitive).

    Implementation:
    - Two parameterised LIKE queries (title-match and body-match) combined with
      UNION to deduplicate — no FTS5, no schema changes.
    - Results ordered by last_activity_at DESC (recency order per spec).
    - match_count: number of posts in the thread whose body_md LIKE q.  Threads
      matched only by title carry match_count=0 (the title itself matched).

    Security:
    - q is passed as a bound parameter only — never interpolated into SQL.
    - like_q (% wrapped) is the SQL LIKE operand; % and _ in q are treated as
      literal search characters via Python-level escaping before wrapping.

    Args:
        conn: Open SQLite connection.
        q:    Raw search term from the user (stripped; must be non-empty).

    Returns:
        List of thread dicts (same shape as list_threads) plus ``match_count``.
        Empty list if q is empty after stripping.
    """
    stripped = q.strip()
    if not stripped:
        return []

    # Escape SQL LIKE metacharacters in the user query so that literal
    # percent/underscore characters don't accidentally act as wildcards.
    escaped = stripped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_q = f"%{escaped}%"

    sql = """
        SELECT
               t.id, t.category_slug, t.title, t.body_md,
               t.pinned, t.unresolved, t.created_at, t.last_activity_at,
               la.name AS last_activity_agent_name,
               a.name AS author_name, a.avatar_seed, a.pair_initials,
               COUNT(p.id) AS reply_count
          FROM threads t
          JOIN agents a  ON a.id  = t.author_agent_id
          JOIN agents la ON la.id = t.last_activity_agent_id
          LEFT JOIN posts p ON p.thread_id = t.id
         WHERE t.id IN (
               -- title match
               SELECT id FROM threads WHERE title LIKE ? ESCAPE '\\'
               UNION
               -- body match (any post in the thread)
               SELECT DISTINCT p2.thread_id
                 FROM posts p2
                WHERE p2.body_md LIKE ? ESCAPE '\\'
         )
         GROUP BY t.id
         ORDER BY t.last_activity_at DESC
    """

    rows = conn.execute(sql, (like_q, like_q)).fetchall()
    result = [_build_thread_dict(conn, r) for r in rows]

    # Annotate each thread with match_count (posts whose body_md LIKE q).
    for td in result:
        count_row = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE thread_id = ? AND body_md LIKE ? ESCAPE '\\'",
            (td["id"], like_q),
        ).fetchone()
        td["match_count"] = count_row[0] if count_row else 0

    return result


# ---------------------------------------------------------------------------
# Hybrid search (slice 2 of #807)
# ---------------------------------------------------------------------------

# Once-per-process degradation log flags (mirrors the #730 pattern).
_HYBRID_DEGRADED_TO_FTS: bool = False
_HYBRID_DEGRADED_TO_LIKE: bool = False


def _fts5_quote(q: str) -> str:
    """Wrap a raw user query as an FTS5 string literal.

    FTS5 MATCH syntax treats bare tokens with special chars (`:`, `-`, `*`,
    `"`) as operators.  Quoting the whole query as a string literal (`"…"`,
    with internal `"` doubled) forces literal-phrase matching and avoids
    syntax errors on adversarial or code-heavy queries.

    Examples::

        _fts5_quote('hello world')  -> '"hello world"'
        _fts5_quote('C++: style')   -> '"C++: style"'
        _fts5_quote('say "yes"')    -> '"say ""yes\"""'
        # Embedded double-quotes are doubled; outer quotes wrap all.
    """
    escaped = q.replace('"', '""')
    return f'"{escaped}"'


def search_threads_hybrid(
    conn: sqlite3.Connection,
    q: str,
    query_vector: "list[float] | None" = None,
    alpha: float = 0.5,
    limit: int = 50,
    expected_rung: str = "hybrid",
) -> "tuple[list[dict[str, Any]], str]":
    """Hybrid search: FTS5 BM25 + vec KNN blend, deduped to threads.

    Score formula: alpha * cosine + (1 - alpha) * bm25_normalized
    where bm25_normalized is per-query min-max to [0,1] (negative-better
    BM25 inverted and rescaled within the candidate set).

    Degradation ladder (loud-once-per-process for STRUCTURAL failures):
      1. hybrid  — FTS5 + semantic arms blended (requires vec tables + vector).
      2. fts     — FTS5 only (query_vector is None or vec tables unavailable).
      3. like    — LIKE fallback via search_threads() (FTS table missing or
                   MATCH error — structural causes only, not empty-hit results).

    Degradation flags (_HYBRID_DEGRADED_TO_FTS / _HYBRID_DEGRADED_TO_LIKE)
    fire ONLY on structural causes (missing table, OperationalError).  An
    explicit lower-rung request (expected_rung='fts') or an empty-result
    fallthrough does NOT consume a flag or emit a warning — those are not
    structural failures.

    Args:
        conn:          Open SQLite connection (with sqlite-vec loaded if
                       available — caller's responsibility, mirrors server.py).
        q:             Raw query string.
        query_vector:  Pre-encoded query embedding from forum/embeddings.py
                       encode().  Pass None when the embedding model is off;
                       the function degrades to FTS-only scoring (alpha→0).
        alpha:         Blend weight [0,1] for the cosine arm.  0 = pure FTS,
                       1 = pure semantic.  Default 0.5.
        limit:         Maximum threads to return.
        expected_rung: The rung the caller explicitly requested ("hybrid",
                       "fts", or "like").  When the caller deliberately
                       requests a lower rung (e.g. "fts"), no degradation
                       flag is set or logged for that choice.

    Returns:
        (results, rung_used) where results is a list of thread dicts (same
        shape as _build_thread_dict + match_count) plus a ``score`` key
        (float, higher is better), ordered score DESC; and rung_used is the
        ladder rung actually executed ("hybrid" | "fts" | "like").
    """
    global _HYBRID_DEGRADED_TO_FTS, _HYBRID_DEGRADED_TO_LIKE

    stripped = q.strip()
    if not stripped:
        return [], expected_rung

    alpha = max(0.0, min(1.0, alpha))
    overfetch = limit * 2  # KNN overfetch — mirrors engram-side idiom

    # ------------------------------------------------------------------
    # FTS arm — posts_fts BM25 candidates
    # ------------------------------------------------------------------
    fts_rows: list[tuple[int, float]] = []  # (post_id, raw_bm25)
    try:
        fts_q = _fts5_quote(stripped)
        rows = conn.execute(
            "SELECT rowid, bm25(posts_fts) FROM posts_fts WHERE posts_fts MATCH ?",
            (fts_q,),
        ).fetchall()
        # bm25() is negative-better in SQLite; collect raw values for
        # normalization within this candidate set (per-query relative scale,
        # fine for blending — not comparable across queries).
        fts_rows = [(r[0], r[1]) for r in rows]
    except sqlite3.OperationalError as _fts_err:
        # Structural failure: posts_fts table missing or MATCH error.
        # This IS a structural degradation — log it once.
        if not _HYBRID_DEGRADED_TO_LIKE:
            _HYBRID_DEGRADED_TO_LIKE = True
            print(
                f"[forum] hybrid-search: posts_fts unavailable or MATCH error "
                f"({_fts_err!r}); falling back to LIKE search. "
                "This is logged once per process.",
                file=sys.stderr,
            )
        return search_threads(conn, stripped), "like"

    # If FTS returned zero results AND no semantic arm, fall through to LIKE.
    # This is a per-query empty-result path — NOT a structural degradation;
    # do not set the flag or log a warning.
    if not fts_rows and query_vector is None:
        return search_threads(conn, stripped), "like"

    # Normalize BM25 scores to [0,1] within candidate set.
    # BM25 is negative-better: most-relevant row has the most-negative value.
    # Invert: fts_norm = (raw - min) / (max - min); min-row → 1.0 (best).
    post_fts_norm: dict[int, float] = {}
    if fts_rows:
        raw_vals = [r[1] for r in fts_rows]
        bm25_min = min(raw_vals)   # most negative = most relevant
        bm25_max = max(raw_vals)   # least negative = least relevant
        span = bm25_max - bm25_min
        for post_id, raw in fts_rows:
            if span == 0.0:
                post_fts_norm[post_id] = 1.0
            else:
                # Invert so that the best (most-negative) score maps to 1.0.
                post_fts_norm[post_id] = (bm25_max - raw) / span

    # ------------------------------------------------------------------
    # Semantic arm — vec_posts + vec_threads KNN
    # ------------------------------------------------------------------
    # vec0 distance metric: for float[384] vec0 uses L2 distance by default.
    # For L2-normalized vectors (unit norm, which embeddings.encode() ensures):
    #   ||a - b||^2 = 2 - 2·cos(a, b)  →  cos = 1 - L2²/2
    # So cosine_similarity = 1 - (l2_distance ** 2) / 2.
    # This identity holds exactly for unit-norm vectors and avoids a separate
    # dot-product query.  See issue #807 design-settlement + embeddings.py.
    post_vec_sim: dict[int, float] = {}   # post_id → cosine similarity
    thread_vec_sim: dict[int, float] = {}  # thread_id → centroid cosine sim

    vec_arm_available = (query_vector is not None) and _VEC_BACKEND_AVAILABLE

    # Log FTS-degradation only when query_vector is unexpectedly absent
    # (i.e. model offline / structural cause) — NOT when the caller explicitly
    # requested the fts rung (expected_rung == "fts").
    if query_vector is None and expected_rung not in ("fts", "like") and not _HYBRID_DEGRADED_TO_FTS:
        _HYBRID_DEGRADED_TO_FTS = True
        print(
            "[forum] hybrid-search: no query vector (model off or "
            "FORUM_NO_EMBEDDINGS set); using FTS-only scoring (alpha→0). "
            "This is logged once per process.",
            file=sys.stderr,
        )

    if vec_arm_available and query_vector is not None:
        try:
            from . import embeddings as _emb
            qvec_blob = _emb.serialize(query_vector)

            # KNN on vec_posts (post-level semantic hits)
            vec_post_rows = conn.execute(
                """
                SELECT post_id, distance
                  FROM vec_posts
                 WHERE embedding MATCH ?
                   AND k = ?
                """,
                (qvec_blob, overfetch),
            ).fetchall()
            for pid, l2_dist in vec_post_rows:
                # Cosine similarity from L2 distance for unit-norm vectors:
                # cos = 1 - L2² / 2  (identity for normalized vectors)
                cos_sim = 1.0 - (l2_dist ** 2) / 2.0
                cos_sim = max(0.0, min(1.0, cos_sim))
                post_vec_sim[pid] = cos_sim

            # KNN on vec_threads (centroid-level semantic hits)
            vec_thread_rows = conn.execute(
                """
                SELECT thread_id, distance
                  FROM vec_threads
                 WHERE embedding MATCH ?
                   AND k = ?
                """,
                (qvec_blob, overfetch),
            ).fetchall()
            for tid, l2_dist in vec_thread_rows:
                cos_sim = 1.0 - (l2_dist ** 2) / 2.0
                cos_sim = max(0.0, min(1.0, cos_sim))
                thread_vec_sim[tid] = cos_sim

        except sqlite3.OperationalError:
            # vec0 tables not present — structural degradation, log once.
            vec_arm_available = False
            if not _HYBRID_DEGRADED_TO_FTS:
                _HYBRID_DEGRADED_TO_FTS = True
                print(
                    "[forum] hybrid-search: vec_posts/vec_threads tables unavailable; "
                    "using FTS-only scoring. This is logged once per process.",
                    file=sys.stderr,
                )

    # ------------------------------------------------------------------
    # Map post hits → thread_id, compute per-thread scores
    # ------------------------------------------------------------------
    # Collect all candidate post IDs from both arms.
    all_post_ids: set[int] = set(post_fts_norm.keys()) | set(post_vec_sim.keys())

    # Map post → thread (one query if we have candidates).
    post_to_thread: dict[int, int] = {}
    if all_post_ids:
        placeholders = ",".join("?" * len(all_post_ids))
        mapping_rows = conn.execute(
            f"SELECT id, thread_id FROM posts WHERE id IN ({placeholders})",
            list(all_post_ids),
        ).fetchall()
        for post_id_val, tid_val in mapping_rows:
            post_to_thread[post_id_val] = tid_val

    # Compute per-post blend score and accumulate max per thread.
    thread_best_post_score: dict[int, float] = {}  # thread_id → max post score

    effective_alpha = alpha if vec_arm_available else 0.0
    for pid in all_post_ids:
        fts_score = post_fts_norm.get(pid, 0.0)
        sem_score = post_vec_sim.get(pid, 0.0)
        blend = effective_alpha * sem_score + (1.0 - effective_alpha) * fts_score
        tid_for_post = post_to_thread.get(pid)
        if tid_for_post is not None:
            if tid_for_post not in thread_best_post_score or blend > thread_best_post_score[tid_for_post]:
                thread_best_post_score[tid_for_post] = blend

    # Centroid contribution: thread_id → centroid_cosine * 0.9
    # Final thread score = max(best_post_score, centroid_score * 0.9)
    all_thread_ids: set[int] = set(thread_best_post_score.keys()) | set(thread_vec_sim.keys())
    thread_final_score: dict[int, float] = {}
    for tid_val in all_thread_ids:
        post_contrib = thread_best_post_score.get(tid_val, 0.0)
        centroid_contrib = thread_vec_sim.get(tid_val, 0.0) * 0.9
        thread_final_score[tid_val] = max(post_contrib, centroid_contrib)

    if not thread_final_score:
        # All candidate sets empty — fall through to LIKE.
        # This is a per-query empty-result path; NOT a structural degradation.
        return search_threads(conn, stripped), "like"

    # ------------------------------------------------------------------
    # Fetch thread rows for matched IDs (standard _build_thread_dict query)
    # ------------------------------------------------------------------
    sorted_tids = sorted(
        thread_final_score.keys(),
        key=lambda t: thread_final_score[t],
        reverse=True,
    )[:limit]

    if not sorted_tids:
        return [], "fts" if not vec_arm_available else "hybrid"

    placeholders = ",".join("?" * len(sorted_tids))
    sql = f"""
        SELECT t.id, t.category_slug, t.title, t.body_md,
               t.pinned, t.unresolved, t.created_at, t.last_activity_at,
               la.name AS last_activity_agent_name,
               a.name AS author_name, a.avatar_seed, a.pair_initials,
               COUNT(p.id) AS reply_count
          FROM threads t
          JOIN agents a  ON a.id  = t.author_agent_id
          JOIN agents la ON la.id = t.last_activity_agent_id
          LEFT JOIN posts p ON p.thread_id = t.id
         WHERE t.id IN ({placeholders})
         GROUP BY t.id
    """
    rows = conn.execute(sql, sorted_tids).fetchall()

    # Build thread dicts in score order (re-sort by our score, since SQL
    # GROUP BY may reorder relative to the sorted_tids list).
    tid_to_row: dict[int, Any] = {r[0]: r for r in rows}
    result: list[dict[str, Any]] = []
    for tid_val in sorted_tids:
        row = tid_to_row.get(tid_val)
        if row is None:
            continue
        td = _build_thread_dict(conn, row)
        td["score"] = thread_final_score[tid_val]

        # match_count: number of posts whose body_md LIKE q (same as search_threads)
        escaped = stripped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_q = f"%{escaped}%"
        count_row = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE thread_id = ? AND body_md LIKE ? ESCAPE '\\'",
            (tid_val, like_q),
        ).fetchone()
        td["match_count"] = count_row[0] if count_row else 0

        result.append(td)

    # Report rung: hybrid if semantic arm was active, fts otherwise.
    rung_used = "hybrid" if vec_arm_available else "fts"
    return result, rung_used


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


def count_unread_by_category(
    conn: sqlite3.Connection,
    agent_id: int,
) -> dict:
    """Count unread posts grouped by category slug (per-thread read-state rollup).

    Returns {slug: count} for categories with at least one unread post.
    Uses the same per-thread watermark logic as count_unread_all_threads.
    Categories with zero unread posts are omitted from the result.
    """
    rows = conn.execute(
        """
        SELECT t.category_slug, COUNT(*)
          FROM posts p
          JOIN threads t ON t.id = p.thread_id
          LEFT JOIN reads r ON r.agent_id = :me AND r.thread_id = p.thread_id
         WHERE p.author_agent_id != :me
           AND p.id > COALESCE(r.last_read_post_id, 0)
         GROUP BY t.category_slug
        """,
        {"me": agent_id},
    ).fetchall()
    return {row[0]: row[1] for row in rows}


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


class ForumInvalidAgentName(ForumBadRequest):
    """Raised when an agent name fails the charset / length check."""


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


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------

def next_pack_version(conn: sqlite3.Connection, author: str, name: str) -> int:
    """Return 1 + max existing version for this author+name, or 1 if new."""
    row = conn.execute(
        "SELECT MAX(version) FROM packs WHERE author = ? AND name = ?",
        (author, name),
    ).fetchone()
    if row is None or row[0] is None:
        return 1
    return int(row[0]) + 1


def insert_pack(
    conn: sqlite3.Connection,
    pack_id: str,
    author: str,
    name: str,
    version: int,
    uploaded_at: str,
    root_count: int,
    node_count: int,
    edge_count: int,
) -> None:
    """Insert a new pack row (pack_id must be unique)."""
    conn.execute(
        "INSERT INTO packs(id, author, name, version, uploaded_at, "
        "                  root_count, node_count, edge_count) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (pack_id, author, name, version, uploaded_at, root_count, node_count, edge_count),
    )
    conn.commit()


def list_packs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all packs ordered by uploaded_at DESC."""
    rows = conn.execute(
        "SELECT id, author, name, version, uploaded_at, "
        "       root_count, node_count, edge_count "
        "  FROM packs "
        " ORDER BY uploaded_at DESC"
    ).fetchall()
    return [
        {
            "id": r[0],
            "author": r[1],
            "name": r[2],
            "version": r[3],
            "uploaded_at": r[4],
            "root_count": r[5],
            "node_count": r[6],
            "edge_count": r[7],
        }
        for r in rows
    ]


def get_pack(conn: sqlite3.Connection, pack_id: str) -> dict[str, Any] | None:
    """Return the pack row dict for pack_id, or None if not found."""
    row = conn.execute(
        "SELECT id, author, name, version, uploaded_at, "
        "       root_count, node_count, edge_count "
        "  FROM packs WHERE id = ?",
        (pack_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "author": row[1],
        "name": row[2],
        "version": row[3],
        "uploaded_at": row[4],
        "root_count": row[5],
        "node_count": row[6],
        "edge_count": row[7],
    }


# ---------------------------------------------------------------------------
# Embedding write helpers (slice 1 of #807)
#
# db.py stays pure-SQL+math: no model dependency here. The embeddings module
# (forum/embeddings.py) handles encode() calls; these helpers handle the writes.
# Failure semantics: callers (server.py) wrap these in try/except so an
# embedding failure never fails the post write.
# ---------------------------------------------------------------------------

def set_post_embedding(
    conn: sqlite3.Connection,
    post_id: int,
    vector: list[float],
) -> None:
    """Write a post's embedding to posts.embedding and mirror into vec_posts.

    vec0 has no UPSERT on virtual tables, so we delete-then-insert to handle
    re-embedding. Mirrors the pattern from server.py:2494-2504.

    Args:
        conn:     Open SQLite connection (sqlite-vec already loaded if available).
        post_id:  The post's integer primary key.
        vector:   Normalized 384-dim float list (serialize with embeddings.serialize).
    """
    from . import embeddings as _emb
    blob = _emb.serialize(vector)
    conn.execute(
        "UPDATE posts SET embedding = ? WHERE id = ?",
        (blob, post_id),
    )
    # Mirror into vec0 KNN index when available.
    if _VEC_BACKEND_AVAILABLE and _sqlite_vec is not None and len(vector) == 384:
        try:
            conn.execute("DELETE FROM vec_posts WHERE post_id = ?", (post_id,))
            conn.execute(
                "INSERT INTO vec_posts(post_id, embedding) VALUES (?, ?)",
                (post_id, blob),
            )
        except sqlite3.Error:
            # Any DB error (missing table, type mismatch, etc.) is non-fatal:
            # the canonical store is posts.embedding; backfill repairs the index.
            pass


def update_thread_centroid(
    conn: sqlite3.Connection,
    thread_id: int,
    post_vector: list[float],
) -> None:
    """Incrementally update a thread's embedding centroid after a new post.

    Formula: new = normalize((old * n + post_vec) / (n + 1))
    where n = number of already-embedded posts in the thread (NOT including
    the new post, which has already been written to posts.embedding by the
    time this is called).

    n is computed by query -- never from a denormalized counter -- so the
    centroid is drift-proof under failed embeds.

    Posts are append-only in this codebase (verified: no UPDATE/DELETE on
    posts in forum/db.py or admin.py).

    APPROXIMATION NOTE: the incremental update is a fast, order-dependent
    approximation of the post-vector mean. It is exact only when a thread's
    post vectors are collinear. In the general case, threads.embedding stores
    only the normalized centroid, discarding the running-sum magnitude; the
    formula treats ‖S_n‖ = n, which is true only if all prior post vectors
    are collinear. Reordering the same set of posts yields a different centroid.

    renormalized_mean() (used by the backfill tool) is the canonical
    order-invariant recompute. Running backfill WILL shift incrementally-built
    centroids on any multi-topic thread. This is expected and acceptable:
    the centroid is a recall signal (scored as centroid·0.9 in slice-2 hybrid
    search), not a reproducibility-critical value.

    Args:
        conn:        Open SQLite connection (sqlite-vec already loaded if available).
        thread_id:   The thread's integer primary key.
        post_vector: Normalized 384-dim float list for the new post.
    """
    from . import embeddings as _emb

    # Count already-embedded posts (the new post was just written, so it IS
    # counted here -- which is correct: n+1 total, n = current count after
    # the write includes the new post embedding).
    # IMPORTANT: we query AFTER set_post_embedding has already stored the new
    # post's embedding, so the count naturally includes it. The incremental
    # formula needs the count BEFORE the new post, so we subtract 1.
    total_embedded = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE thread_id = ? AND embedding IS NOT NULL",
        (thread_id,),
    ).fetchone()[0]
    n_before = total_embedded - 1  # exclude the post we just wrote

    # Fetch the current thread centroid (may be NULL for first post).
    row = conn.execute(
        "SELECT embedding FROM threads WHERE id = ?", (thread_id,)
    ).fetchone()

    if row is None or row[0] is None or n_before <= 0:
        # <= 0 (not == 0): defensive against n_before going negative when the new
        # post's own embedding write failed, leaving total_embedded one less than
        # expected (would produce n_before = -1 without this guard).
        # First embedded post: centroid IS the post vector (already normalized).
        new_centroid = list(post_vector)
    else:
        old_centroid = _emb.deserialize(row[0])
        new_centroid = _emb.incremental_centroid(old_centroid, n_before, post_vector)

    blob = _emb.serialize(new_centroid)
    conn.execute(
        "UPDATE threads SET embedding = ? WHERE id = ?",
        (blob, thread_id),
    )
    # Mirror into vec0 KNN index when available.
    if _VEC_BACKEND_AVAILABLE and _sqlite_vec is not None and len(new_centroid) == 384:
        try:
            conn.execute("DELETE FROM vec_threads WHERE thread_id = ?", (thread_id,))
            conn.execute(
                "INSERT INTO vec_threads(thread_id, embedding) VALUES (?, ?)",
                (thread_id, blob),
            )
        except sqlite3.OperationalError:
            pass  # vec_threads missing -- embedding column is the canonical store
