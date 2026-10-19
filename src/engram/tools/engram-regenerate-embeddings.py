#!/usr/bin/env python3
"""engram-regenerate-embeddings.py — backfill NULL embeddings after a git restore.

Usage:
    python tools/engram-regenerate-embeddings.py [--engram-home PATH] [--json]

    --json: after the normal backfill work, print exactly one JSON object to
    stdout: {"before_null", "embedded", "failed", "after_null"} (#345 PR1
    part (c)). before_null is the same is_current=1/embedding-IS-NULL count
    this script always used; after_null is RE-QUERIED post-run, not computed
    by subtraction. stderr progress is unchanged; without --json, stdout is
    unchanged from today. Consumed by the engram-sleep Phase A embedding-
    backfill step so the backfill this issue's store-and-flag design relies
    on actually runs routinely.

When to run:
    After restoring knowledge.db from the git-tracked SQL dump:
        sqlite3 "${ENGRAM_HOME:-$HOME/.engram}"/knowledge.db < "${ENGRAM_HOME:-$HOME/.engram}"/knowledge.sql
    The dump excludes embeddings (they are regenerable and their inclusion
    caused ~37 MB churn per commit). This script backfills them so semantic
    search works again.

What it does:
    For every is_current=1 node with embedding IS NULL, computes the embedding
    from the claim text (using server.py's _embedding_text_for_node + the same
    sentence-transformers model the server uses) and writes it back. Also
    updates the vec_nodes KNN index if sqlite-vec is available.

Idempotent:
    Only touches nodes where embedding IS NULL — safe to run multiple times.
    Nodes that already have an embedding are skipped.

Progress:
    Logs to stderr: total to backfill, count per batch, final summary.

Restore procedure (full):
    1. sqlite3 "${ENGRAM_HOME:-$HOME/.engram}"/knowledge.db < "${ENGRAM_HOME:-$HOME/.engram}"/knowledge.sql
    2. python tools/engram-regenerate-embeddings.py
    3. Restart the MCP server (so it reloads the fresh DB + vec index).

    With engramd.regen_embeddings_via_owner ON, restoring from the SQL dump is still an OFFLINE act
    on the store file, so the order is (#3879):
    1. stop the engramd owner
    2. restore: sqlite3 "$ENGRAM_HOME/knowledge.db" < "$ENGRAM_HOME/knowledge.sql"
    3. start the engramd owner
    4. regenerate: python tools/engram-regenerate-embeddings.py
    5. Restart the MCP server.

Writing through the engramd owner (engramd.regen_embeddings_via_owner, default OFF; #3879, U16 of #2652):
    Flag OFF: everything above, unchanged. Flag ON (config.json "engramd": {"regen_embeddings_via_owner":
    true}, applied to THIS ENGRAM_HOME's store): the tool never opens knowledge.db. It still loads the
    server runtime and the embedder to COMPUTE vectors (the model stays out of the owner), but
      - every read (the NULL count, the NULL id list, each node's embedding-text row) goes through the
        owner's registered read templates, and after_null is a fresh read, never a subtraction;
      - the vectors are written by the owner's `fill_embeddings` op (batches of at most 50; a
        compare-and-set that never overwrites a vector something else wrote meanwhile), then exactly one
        `finish_embedding_regen` op rebuilds the FTS index and runs the restore-flag clear-or-finalize
        branch in one transaction. The finish op carries `embedder_available`, this tool's own
        `_embedder.is_available()` probe (the owner never probes an embedder; it takes the flag cause from
        that value).
    `embedded` counts what the owner reports as filled. An ack whose filled + skipped_not_null +
    skipped_not_current_or_missing does not equal the number of items sent in that batch is a malformed
    answer (exit 3). Nodes the owner skipped because they already hold
    a vector (skipped_not_null) or are no longer current are reported on stderr and are not failures. A
    batch the owner could not mirror into vec_nodes (vec_mirrored: false) is a stderr WARNING.
    `vec_mirrored: false` is reported as that stderr WARNING only (exit 0, --json unchanged), by design,
    matching the direct path when vec0 cannot load.

Exit codes:
    0  done (including a run with nothing to embed)
    1  one or more nodes failed to embed (failed > 0); the --json object is printed first
    3  EXIT_OWNER_FAILURE: flag ON and the owner is down, refusing, answered malformed, or the outcome of
       a write is indeterminate. stderr's LAST line names the reason, NO JSON is printed, and the tool
       falls back to nothing: it does not touch the store directly. Re-running is safe (the fill is a
       compare-and-set). engram-sleep Step 3.9 reads any non-zero exit, or empty stdout, as a FAILED line.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

# append (not insert(0)) -- #2565: same reasoning as tools/backup_knowledge_db.py (the tools/-package-name
# clash): src/engram at sys.path POSITION 0 would outrank the repo-root tools/ package for the rest of the
# process.
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from tools import _owner_read as owner_read  # noqa: E402

# ---------------------------------------------------------------------------
# Owner mode (#3879, U16 of #2652)
# ---------------------------------------------------------------------------

OWNER_FLAG_KEY = "regen_embeddings_via_owner"
# Distinct from 1 (failed > 0) so a caller can tell "some nodes did not embed" from "the owner could not be
# used". engram-sleep Step 3.9 reads any non-zero exit as FAILED.
EXIT_OWNER_FAILURE = 3


class RegenOwnerError(RuntimeError):
    """The owner could not do the regeneration's reads or writes (flag ON). Raised, never answered from a
    direct open of the live store: a regeneration that silently came from somewhere else is the failure this
    exists to prevent."""


def _owner_spec(engram_home: Path) -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=RegenOwnerError,
        home=engram_home,
        never_empty="Did not fall back to the live store directly: nothing was regenerated.",
    )

# ---------------------------------------------------------------------------
# Locate server.py — probe plugin runtime dir first, then scatter fallback,
# then repo-adjacent.  Priority order matters: on a plugin install the
# canonical server.py lives at engram_home/marketplace/plugins/engram/;
# the legacy scatter copy at engram_home/server.py is an inert leftover that
# may not be current (and is absent on a clean plugin install).
# ---------------------------------------------------------------------------

def _find_server_py(engram_home: Path) -> Path | None:
    """Find server.py in priority order.

    Checks in priority order:
    1. Plugin runtime dir (engram_home/marketplace/plugins/engram/) — canonical
       on plugin installs; preferred over any scatter leftover.
    2. ENGRAM_HOME directly (engram_home/server.py) — scatter leftover or a
       non-standard layout; absent on a clean plugin install.
    3. Repo root (parent of tools/) — dev-repo layout fallback.
    """
    candidates = [
        engram_home / "marketplace" / "plugins" / "engram" / "server.py",
        engram_home / "server.py",
        Path(__file__).resolve().parent.parent / "server.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _find_engram_backup_dir(engram_home: Path, server_py: Path) -> Path | None:
    """Find the directory that contains a current engram_backup.py.

    Checks in priority order:
    1. Plugin runtime dir (engram_home/marketplace/plugins/engram/) — canonical
       on plugin installs; preferred even when a stale scatter server.py was
       found by _find_server_py.
    2. Same dir as the located server.py — correct for dev-repo layout and for
       any install where server.py and engram_backup.py co-deploy together.
    3. Repo root (parent of tools/) — fallback for dev runs without ENGRAM_HOME.
    """
    candidates = [
        engram_home / "marketplace" / "plugins" / "engram",
        server_py.parent,
        Path(__file__).resolve().parent.parent,
    ]
    for d in candidates:
        if (d / "engram_backup.py").exists():
            return d
    return None


def _import_server(engram_home: Path):
    """Import server.py and configure it to use engram_home."""
    server_py = _find_server_py(engram_home)
    if server_py is None:
        print(
            "ERROR: could not locate server.py. "
            "Set ENGRAM_HOME or run from the repo root.",
            file=sys.stderr,
        )
        sys.exit(1)
    if str(server_py.parent) not in sys.path:
        sys.path.insert(0, str(server_py.parent))
    # Ensure engram_backup.py is importable: find its directory and prepend it
    # to sys.path.  The plugin layout keeps engram_backup.py in the plugin
    # runtime dir (engram_home/marketplace/plugins/engram/), which may differ
    # from server_py.parent when _find_server_py resolves a stale scatter copy
    # of server.py from engram_home directly.
    backup_dir = _find_engram_backup_dir(engram_home, server_py)
    if backup_dir is not None and str(backup_dir) not in sys.path:
        sys.path.insert(0, str(backup_dir))
    import importlib
    # Purge server AND engram_core as a pair (#872 wave 1): the resolved
    # server tree must load its own matching core — a cached engram_core
    # from another tree/version would carry foreign path state and a
    # version-incoherent module pair.
    for _mod in ("server", "engram_core"):
        sys.modules.pop(_mod, None)
    import server
    server._configure_paths(engram_home)
    return server


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------

BATCH_SIZE = 50  # embed this many texts per sentence-transformers call


def _backfill_embeddings_core(conn, server, model_name: str, vec_available: bool) -> dict:
    """Core NULL-only backfill loop (#3075 C4).

    Assumes `conn` already has row_factory=sqlite3.Row set — this function's
    own dict-style row access (`r["id"]`) depends on it, same as it always
    has; unlike server._embedding_text_for_node (made independent of the
    caller's row_factory by #3075 C1), this loop is explicitly OUT of C1's
    scope to touch.

    Returns a dict with keys 'processed', 'embedded', 'skipped', 'failed':
      - 'processed': every is_current=1/embedding-IS-NULL node found at the
        start of the pass.
      - 'skipped': nodes with no embeddable text (evidence with no
        source_title, or empty claim) — expected, not a failure.
      - 'embedded': nodes whose vector was computed and written.
      - 'failed': nodes where embed_batch returned None for the whole batch,
        or an individual vector came back empty/None.

    Does NOT sys.exit — the caller (backfill_embeddings) owns the fail-loud
    exit-code contract, after printing its own summary.
    """
    null_count = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
    ).fetchone()[0]

    counts = {"processed": null_count, "embedded": 0, "skipped": 0, "failed": 0}
    if null_count == 0:
        return counts

    rows = conn.execute(
        "SELECT id FROM nodes WHERE is_current=1 AND embedding IS NULL ORDER BY id"
    ).fetchall()

    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch_ids = [r["id"] for r in rows[batch_start: batch_start + BATCH_SIZE]]
        texts = []
        valid_ids = []

        for node_id in batch_ids:
            text = server._embedding_text_for_node(conn, node_id)
            if text:
                texts.append(text)
                valid_ids.append(node_id)
            else:
                counts["skipped"] += 1

        if not texts:
            continue

        # Batch embed
        vectors = server._embedder.embed_batch(texts, model_name)
        if vectors is None:
            counts["failed"] += len(valid_ids)
            print(
                f"[regen] WARNING: embed_batch returned None for batch starting at {batch_start}",
                file=sys.stderr,
            )
            continue

        for node_id, vector in zip(valid_ids, vectors):
            if not vector:
                counts["failed"] += 1
                continue
            embedding_json = json.dumps(vector)
            conn.execute(
                "UPDATE nodes SET embedding = ? WHERE id = ?",
                (embedding_json, node_id),
            )
            # Mirror into vec_nodes KNN index if available
            if vec_available and len(vector) == 384:
                try:
                    import sqlite_vec as _sv2
                    conn.execute(
                        "DELETE FROM vec_nodes WHERE node_id = ?", (node_id,)
                    )
                    conn.execute(
                        "INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                        (node_id, _sv2.serialize_float32(vector)),
                    )
                except Exception:
                    pass  # vec_nodes missing or locked — JSON column is canonical

            counts["embedded"] += 1

        conn.commit()
        print(
            f"[regen] Batch {batch_start // BATCH_SIZE + 1}: "
            f"attempted {len(valid_ids)} node(s) "
            f"(cumulative embedded: {counts['embedded']}/{null_count})",
            file=sys.stderr,
        )

    return counts


# ---------------------------------------------------------------------------
# Owner mode: reads through registered templates, writes through typed owner ops
# ---------------------------------------------------------------------------
#
# The three reads below are the ONLY statements the flag-ON path sends. Each is a literal, registered
# template in src/engram/engramd_read_templates.json (site tools/engram-regenerate-embeddings.py:<function>).
# The text row's SELECT is byte-equal to the one engram_core._embedding_text_for_node issues (pinned by
# tests/test_regen_embeddings_owner_3879.py), so the embedding text is built by that SAME function, not by
# a second copy of its type rules.

_TEXT_ROW_COLUMNS = (
    "id TEXT, type TEXT, claim TEXT, source_title TEXT, predicted_event TEXT, "
    "recall_summary TEXT, recall_keywords TEXT"
)


def _cell_int(row, what: str) -> int:
    value = row[0] if row else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegenOwnerError(f"the engramd owner answered the {what} read malformed (not an integer)")
    return value


# Answer-shape contract (#3879, PR #3900 security review): each read validates ITS OWN shape and uses
# fetchall() (never fetchone(), which silently drops extra rows). Any violation is a RegenOwnerError -> exit 3,
# no JSON, the message names the read.
#   COUNT      exactly 1 row, that row exactly 1 cell, an int (not a bool), >= 0. Used for BOTH the initial
#              count and the final after_null.
#   ID list    0+ rows, every row exactly 1 str cell, no id repeated (the query is over a primary key).
#   text row   0 or 1 rows; a row is exactly 6 cells, each str or None.


def _owner_null_count(reader) -> int:
    what = "NULL-embedding count"
    rows = reader.execute(
        "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
    ).fetchall()
    if len(rows) != 1 or len(rows[0]) != 1:
        raise RegenOwnerError(f"the engramd owner answered the {what} read malformed (not exactly 1 row x 1 cell)")
    value = _cell_int(rows[0], what)
    if value < 0:
        raise RegenOwnerError(f"the engramd owner answered the {what} read malformed (negative count)")
    return value


def _owner_null_ids(reader) -> list:
    rows = reader.execute(
        "SELECT id FROM nodes WHERE is_current=1 AND embedding IS NULL ORDER BY id"
    ).fetchall()
    if any(len(r) != 1 for r in rows):
        raise RegenOwnerError("the engramd owner answered the NULL-embedding id read malformed (a row is not 1 cell)")
    ids = [r[0] for r in rows]
    if not all(isinstance(i, str) for i in ids):
        raise RegenOwnerError("the engramd owner answered the NULL-embedding id read malformed (a non-string id)")
    if len(set(ids)) != len(ids):
        raise RegenOwnerError("the engramd owner answered the NULL-embedding id read malformed (a duplicate id)")
    return ids


def _owner_text_row(reader, node_id: str):
    rows = reader.execute(
        "SELECT type, claim, source_title, predicted_event, recall_summary, recall_keywords "
        "FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchall()
    if len(rows) > 1:
        raise RegenOwnerError("the engramd owner answered the embedding-text read malformed (more than one row)")
    if not rows:
        return None
    row = rows[0]
    if len(row) != 6:
        raise RegenOwnerError("the engramd owner answered the embedding-text read malformed (wrong column count)")
    if not all(c is None or isinstance(c, str) for c in row):
        raise RegenOwnerError("the engramd owner answered the embedding-text read malformed (a non-text cell)")
    return row


def _owner_texts(server, reader, batch_ids: list) -> list:
    """The embedding text per id (\"\" when there is none), built by server._embedding_text_for_node over a
    private in-memory copy of the one row the owner returned. Never a connection to the live store."""
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(f"CREATE TABLE nodes ({_TEXT_ROW_COLUMNS})")
        texts = []
        for node_id in batch_ids:
            row = _owner_text_row(reader, node_id)
            if row is None:  # the node vanished since the id list: the direct path's "" -> skipped
                texts.append("")
                continue
            mem.execute("DELETE FROM nodes")
            mem.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)", (node_id, *row))
            texts.append(server._embedding_text_for_node(mem, node_id))
        return texts
    finally:
        mem.close()


def _int_field(ack: dict, key: str, op: str) -> int:
    value = ack.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RegenOwnerError(f"the engramd owner answered the {op} write malformed ({key} is not a count)")
    return value


def _owner_fill(spec, model_name: str, items: list) -> dict:
    ack = owner_read.write(
        spec, "fill_embeddings", {"model": model_name, "items": items}, request_id=str(uuid.uuid4())
    )
    if not isinstance(ack.get("vec_mirrored"), bool):
        raise RegenOwnerError("the engramd owner answered the fill_embeddings write malformed (vec_mirrored)")
    out = {
        "filled": _int_field(ack, "filled", "fill_embeddings"),
        "skipped_not_null": _int_field(ack, "skipped_not_null", "fill_embeddings"),
        "skipped_not_current_or_missing": _int_field(ack, "skipped_not_current_or_missing", "fill_embeddings"),
        "vec_mirrored": ack["vec_mirrored"],
    }
    # Every item sent was classified exactly once by the owner (filled / skipped_not_null /
    # skipped_not_current_or_missing). A sum that is not the number SENT in this batch means the owner dropped
    # or invented an item: believing it would let `embedded` and the exit code lie, so it is a malformed answer.
    accounted = out["filled"] + out["skipped_not_null"] + out["skipped_not_current_or_missing"]
    if accounted != len(items):
        raise RegenOwnerError(
            f"the engramd owner answered the fill_embeddings write malformed (the ack accounts for {accounted} "
            f"of {len(items)} items: filled + skipped_not_null + skipped_not_current_or_missing must equal the "
            "items sent in the batch)"
        )
    return out


def _owner_finish(spec, counts: dict, embedder_available: bool) -> dict:
    """`embedder_available` is the TOOL's probe (the embedder it just used): the owner picks the restore-flag cause
    from it and never probes an embedder itself, so the model stays out of the owner process."""
    ack = owner_read.write(
        spec,
        "finish_embedding_regen",
        {**{key: counts[key] for key in ("processed", "embedded", "skipped", "failed")},
         "embedder_available": embedder_available},
        request_id=str(uuid.uuid4()),
    )
    state = ack.get("restore_state")
    if state is not None and not isinstance(state, str):
        raise RegenOwnerError("the engramd owner answered the finish_embedding_regen write malformed (restore_state)")
    return {"fts_rows": _int_field(ack, "fts_rows", "finish_embedding_regen"), "restore_state": state}


def _owner_fail(exc: Exception, written: int) -> None:
    """Exit EXIT_OWNER_FAILURE: the reason is the LAST stderr line (what engram-sleep Step 3.9 records), no
    JSON, nothing else attempted."""
    if written:
        print(
            f"[regen] NOTE: {written} embedding(s) were already written through the owner before this "
            "failure; re-running is safe (the fill is a compare-and-set).",
            file=sys.stderr,
        )
    reason = " ".join(str(exc).split())
    print(f"[regen] ERROR: engramd owner failure (exit {EXIT_OWNER_FAILURE}): {reason}", file=sys.stderr)
    sys.exit(EXIT_OWNER_FAILURE)


def _backfill_via_owner(server, spec, db_path: Path, model_name: str, json_output: bool) -> None:
    """The flag-ON body of backfill_embeddings. Same stderr lines in the same order, same Done line, same
    --json object and exit-1-on-failed contract as the direct path; see the module docstring."""
    progress = {"written": 0}
    try:
        counts = _owner_run(server, spec, db_path, model_name, json_output, progress)
    except RegenOwnerError as exc:
        _owner_fail(exc, progress["written"])
    if counts["failed"] > 0:
        sys.exit(1)


def _owner_run(server, spec, db_path: Path, model_name: str, json_output: bool, progress: dict) -> dict:
    reader = owner_read.open_reader(spec, db_path, True)
    try:
        null_count = _owner_null_count(reader)
        ids = _owner_null_ids(reader) if null_count else []
    finally:
        reader.close()

    counts = {"processed": null_count, "embedded": 0, "skipped": 0, "failed": 0}
    if null_count == 0:
        print("[regen] No nodes with NULL embedding — nothing to do.", file=sys.stderr)
    for batch_start in range(0, len(ids), BATCH_SIZE):
        batch_ids = ids[batch_start: batch_start + BATCH_SIZE]
        reader = owner_read.open_reader(spec, db_path, True)  # a fresh read budget per batch
        try:
            batch_texts = _owner_texts(server, reader, batch_ids)
        finally:
            reader.close()
        texts, valid_ids = [], []
        for node_id, text in zip(batch_ids, batch_texts):
            if text:
                texts.append(text)
                valid_ids.append(node_id)
            else:
                counts["skipped"] += 1
        if not texts:
            continue

        vectors = server._embedder.embed_batch(texts, model_name)
        if vectors is None:
            counts["failed"] += len(valid_ids)
            print(
                f"[regen] WARNING: embed_batch returned None for batch starting at {batch_start}",
                file=sys.stderr,
            )
            continue

        items = []
        for node_id, vector in zip(valid_ids, vectors):
            if not vector:
                counts["failed"] += 1
                continue
            items.append({"node_id": node_id, "vector": [float(x) for x in vector]})

        batch_no = batch_start // BATCH_SIZE + 1
        if items:
            ack = _owner_fill(spec, model_name, items)
            counts["embedded"] += ack["filled"]
            progress["written"] += ack["filled"]
        print(
            f"[regen] Batch {batch_no}: attempted {len(valid_ids)} node(s) "
            f"(cumulative embedded: {counts['embedded']}/{null_count})",
            file=sys.stderr,
        )
        if items:
            if ack["skipped_not_null"] or ack["skipped_not_current_or_missing"]:
                print(
                    f"[regen] Batch {batch_no}: the owner skipped {ack['skipped_not_null']} node(s) that "
                    f"already hold a vector (skipped_not_null) and "
                    f"{ack['skipped_not_current_or_missing']} that are no longer current or are gone "
                    "(skipped_not_current_or_missing) — not failures.",
                    file=sys.stderr,
                )
            if not ack["vec_mirrored"]:
                print(
                    f"[regen] WARNING: the owner could not mirror batch {batch_no} into vec_nodes "
                    "(vec_mirrored=false); the nodes.embedding column is canonical, but KNN search will "
                    "not see these rows.",
                    file=sys.stderr,
                )

    if counts["skipped"] > 0:
        print(
            "[regen] Nodes with no embeddable text (evidence with no source_title, "
            "or empty claim) were skipped — this is expected for some node types.",
            file=sys.stderr,
        )

    # One finish op for every run, the nothing-to-do run included: it rebuilds the FTS index and runs the
    # clear-or-finalize branch of the restore flag, exactly as the direct path does on both branches.
    # The probe is taken HERE, the point at which the direct path's finalize evaluates it (after the pass and the
    # FTS rebuild step, immediately before the flag is decided), and from this process's embedder.
    _owner_finish(spec, counts, bool(server._embedder.is_available()))
    print("[regen] FTS index rebuild complete.", file=sys.stderr)
    if null_count and counts["failed"] == 0:
        print(
            "[regen] Restart the MCP server to reload the vec index: "
            "pkill -f 'python.*server.py'",
            file=sys.stderr,
        )
    print(
        f"[regen] Done. processed={counts['processed']} "
        f"embedded={counts['embedded']} skipped={counts['skipped']} "
        f"failed={counts['failed']}",
        file=sys.stderr,
    )

    if json_output:
        # after_null is a FRESH owner read, never before_null - embedded (same rule as the direct path).
        reader = owner_read.open_reader(spec, db_path, True)
        try:
            after_null = _owner_null_count(reader)
        finally:
            reader.close()
        print(json.dumps({
            "before_null": null_count,
            "embedded": counts["embedded"],
            "failed": counts["failed"],
            "after_null": after_null,
        }))
    return counts


def backfill_embeddings(engram_home: Path, json_output: bool = False) -> None:
    db_path = engram_home / "knowledge.db"
    spec = _owner_spec(engram_home)
    use_owner = owner_read.flag_applies(spec, db_path)
    if use_owner:
        # Flag ON replaces `db_path.exists()`: the owner must answer. Before the server runtime and the
        # model are loaded, so an owner outage costs nothing.
        try:
            owner_read.ping(spec)
        except RegenOwnerError as exc:
            _owner_fail(exc, 0)
    else:
        # Kept: it must run before `_import_server` and the model load below, and it prints this tool's own
        # remedy lines (the create-the-store line for not-found only).
        try:
            owner_read.check_store_path(db_path)
        except FileNotFoundError:
            print(
                f"ERROR: knowledge.db not found at {db_path}\n"
                "  Run: sqlite3 \"${ENGRAM_HOME:-$HOME/.engram}/knowledge.db\" < \"${ENGRAM_HOME:-$HOME/.engram}/knowledge.sql\"  first.",
                file=sys.stderr,
            )
            sys.exit(1)
        except owner_read.StoreUnusable as exc:
            # #3960: the remedy for these classes is to fix the path, never to create a store there, so no
            # "Run: sqlite3" line. Exit 1, never EXIT_OWNER_FAILURE: the flag is OFF, the owner did not fail.
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    server = _import_server(engram_home)

    # Check embedder availability
    if not server._embedder.is_available():
        print(
            "ERROR: sentence-transformers is not installed. "
            "Install it with: pip install sentence-transformers",
            file=sys.stderr,
        )
        sys.exit(1)

    emb_config = server._get_embedding_config()
    if not emb_config.get("enabled", True):
        print(
            "WARNING: config.json carries embedding.enabled=false. That "
            "key was retired by #345 (embeddings are mandatory) and the "
            "MCP server now refuses to start while it is set. Proceeding "
            "with the backfill; delete the key from config.json.",
            file=sys.stderr,
        )

    model_name = emb_config.get("model", server.DEFAULT_EMBEDDING_MODEL)
    print(f"[regen] Using model: {model_name}", file=sys.stderr)

    # Load model upfront (shows a clear error if the model isn't cached)
    server._embedder._load_model(model_name)
    if server._embedder._model is None:
        print(
            f"ERROR: could not load embedding model '{model_name}'. "
            "Check that the model is cached locally via sentence-transformers.",
            file=sys.stderr,
        )
        sys.exit(1)

    if use_owner:
        _backfill_via_owner(server, spec, db_path, model_name, json_output)
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Load sqlite-vec extension if available (mirrors server.py startup)
    try:
        import sqlite_vec as _sv
        conn.enable_load_extension(True)
        _sv.load(conn)
        vec_available = True
    except Exception:
        vec_available = False

    try:
        # #3075 C4: the core NULL-only backfill loop now lives in
        # _backfill_embeddings_core (unit-testable in isolation) — this
        # function does setup, reports the outcome, and owns the fail-loud
        # exit-code contract.
        counts = _backfill_embeddings_core(conn, server, model_name, vec_available)
        # C1 (#345 PR1 part (c)): before_null is the SAME predicate
        # _backfill_embeddings_core already used to populate counts
        # ["processed"], queried before any UPDATE in this pass touched a
        # row -- reusing it avoids a redundant round trip rather than
        # re-issuing the identical SELECT.
        before_null = counts["processed"]

        if counts["processed"] == 0:
            print("[regen] No nodes with NULL embedding — nothing to do.", file=sys.stderr)
            import engram_backup  # available after _import_server inserted the backup module dir on sys.path
            if engram_backup.rebuild_fts_index(str(db_path)):
                print("[regen] FTS index rebuild complete.", file=sys.stderr)
            else:
                print("[regen] no nodes_fts table present — skipping FTS rebuild.", file=sys.stderr)
            print(
                "[regen] Done. processed=0 embedded=0 skipped=0 failed=0",
                file=sys.stderr,
            )
            # #3075 C5 round 2 (judgment call, documented in the PR handoff
            # — not explicitly named in Borges's ruling, but required to
            # close the loop for the MANUAL restore path the same way the
            # async server-side path does): nothing to do here means no
            # NULL embeddings exist, so any restore-completeness flag left
            # set by an earlier crashed/failed attempt is stale — clear it.
            try:
                # #3077 round 4 (Borges's colleague-pass required fix):
                # this script never acquires the regeneration lease
                # itself, so a fresh "regeneration-running" lease found
                # here is definitionally someone else's. round 2's
                # separate `if not fresh_lease_held(conn): <write>`
                # pre-check was a check-then-act TOCTOU (a lease acquired
                # in the window between the read and this write would be
                # silently clobbered) -- fixed by folding the guard into
                # the write's own atomic SQL instead
                # (respect_lease=True, see _clear_embedding_restore_flag's
                # docstring).
                server.core._clear_embedding_restore_flag(conn, respect_lease=True)
            except Exception:
                pass  # best-effort — never fail the script over flag bookkeeping
            if json_output:
                # C1: after_null is RE-QUERIED (not before_null - embedded)
                # even on the nothing-to-do path, for the same reason the
                # normal-completion path below re-queries -- a uniform rule
                # is simpler to verify than "subtract except when empty."
                after_null = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
                ).fetchone()[0]
                print(json.dumps({
                    "before_null": before_null,
                    "embedded": counts["embedded"],
                    "failed": counts["failed"],
                    "after_null": after_null,
                }))
            return

        if counts["skipped"] > 0:
            print(
                "[regen] Nodes with no embeddable text (evidence with no source_title, "
                "or empty claim) were skipped — this is expected for some node types.",
                file=sys.stderr,
            )

        import engram_backup  # available after _import_server inserted the backup module dir on sys.path
        if engram_backup.rebuild_fts_index(str(db_path)):
            print("[regen] FTS index rebuild complete.", file=sys.stderr)
        else:
            print("[regen] no nodes_fts table present — skipping FTS rebuild.", file=sys.stderr)

        if counts["failed"] == 0:
            print(
                "[regen] Restart the MCP server to reload the vec index: "
                "pkill -f 'python.*server.py'",
                file=sys.stderr,
            )

        # #3075 C4: the final stderr line is ALWAYS the four-count summary —
        # printed last (after every other diagnostic above) so a reader/
        # script scanning stderr's tail sees the authoritative outcome.
        print(
            f"[regen] Done. processed={counts['processed']} "
            f"embedded={counts['embedded']} skipped={counts['skipped']} "
            f"failed={counts['failed']}",
            file=sys.stderr,
        )

        # #3075 C5 round 2 (judgment call, documented in the PR handoff —
        # same reasoning as the processed==0 branch above): close the loop
        # on the DURABLE restore-completeness flag for the manual restore
        # path, not just the automatic server-side one. "Regeneration
        # clears it on completion" (Borges's ruling) applies regardless of
        # WHICH regeneration path did the work.
        #
        # Round 3: this used to re-count remaining NULL rows directly and
        # set the flag unconditionally on any remainder — the same
        # skip-vs-fail bug _run_embedding_restore_regeneration had (see
        # _finalize_embedding_restore_flag's own docstring). Fixed by
        # routing through that same shared helper with the `counts` dict
        # already in scope here, so a permanent empty-text skip (e.g. an
        # evidence node with no source_title -- the exact case the print
        # a few lines above already calls "expected") no longer leaves
        # this flag permanently stuck with an unhelpful remedy.
        try:
            # #3077 round 4 (Borges's colleague-pass required fix): same
            # reasoning as the processed==0 branch above -- this script
            # never acquires the lease itself, so the finalize write must
            # respect a genuinely fresh lease. respect_lease=True on THIS
            # call folds the guard into _finalize_embedding_restore_flag's
            # own atomic writes, rather than a separate check-then-act
            # pre-check around the whole call (round 2's TOCTOU).
            server.core._finalize_embedding_restore_flag(
                conn, counts, respect_lease=True,
            )
        except Exception:
            pass  # best-effort — never fail the script over flag bookkeeping

        if json_output:
            # C1 (#345 PR1 part (c)): after_null is RE-QUERIED from the DB
            # here, deliberately NOT computed as before_null - embedded --
            # this is the one line that keeps the summary honest if
            # anything else ever touches nodes.embedding between the
            # backfill loop finishing and this point.
            after_null = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            print(json.dumps({
                "before_null": before_null,
                "embedded": counts["embedded"],
                "failed": counts["failed"],
                "after_null": after_null,
            }))

        if counts["failed"] > 0:
            # #3075 C4 / Lei's C5 ruling: fail LOUD, not silently — a
            # non-zero exit is the one signal a caller (a human, or a
            # wrapping script) cannot miss.
            sys.exit(1)

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backfill NULL embeddings after a git-restore of knowledge.db."
    )
    parser.add_argument(
        "--engram-home",
        default=str(owner_read.engram_home()),
        help="Path to the ENGRAM data directory (default: $ENGRAM_HOME or ~/.engram)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "After the normal backfill work, print exactly one JSON object "
            "to stdout: {\"before_null\", \"embedded\", \"failed\", "
            "\"after_null\"} (#345 PR1 part (c), for the engram-sleep "
            "Phase A embedding-backfill step). stderr progress is "
            "unchanged; without this flag, stdout is unchanged from today."
        ),
    )
    args = parser.parse_args()
    engram_home = Path(args.engram_home).expanduser().resolve()
    backfill_embeddings(engram_home, json_output=args.json)


if __name__ == "__main__":
    main()
