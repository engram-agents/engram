#!/usr/bin/env python3
"""ENGRAM Diagnostic CLI — raw access to agent-facing-stripped fields.

Fields like `embedding`, `confidence_history`, `content_hash`, and `git_sha`
are stripped from MCP tool returns (issue #358) because they are opaque to
agent reasoning. This script provides direct DB access to those fields when
genuinely needed for debugging.

Usage:
    python inspect_raw.py <node_id>
        List available raw fields for the node (those stripped from MCP returns).

    python inspect_raw.py <node_id> --field <name>
        Print the raw value of a specific stripped field.

    python inspect_raw.py <node_id> --field embedding --json
        Print the value as JSON (useful for piping to other tools).

Available stripped fields:
    embedding          384-float JSON array (cosine similarity target)
    confidence_history List of {timestamp, value, reason} dicts
                       (first entries written before #3994 for conjectures
                       and axioms use the key `confidence` instead of `value`)
    content_hash       SHA-256 of the source file content (file:// obs only)
    git_sha            Git commit SHA when the file was read (file:// obs only)
    parsed_metadata    Full parsed metadata dict (superset of metadata string)

Honors ENGRAM_HOME environment variable (same pattern as server.py).
Does NOT honor the #348 hardcoded-path bug — uses the env-var correctly.

Reading the row (#2652, tools-7): the tool reads ONE row, through the shared owner-read helper
(tools/_owner_read.py). Behind engramd.inspect_raw_reads_via_owner (default OFF) the read goes to the
engramd owner; with the flag OFF the store is opened mode=ro. Neither path holds a write-capable handle.
With the flag ON and the owner down, refusing or answering malformed, the tool exits 3 with an empty
stdout and one stderr line (it never says "not found" and never falls back to a direct open). Exit
codes: 0 ok, 1 node or store not found (or an unknown field), 2 usage (argparse), a RELATIVE ENGRAM_HOME
(refused with one stderr line, as everywhere in the repo, alpha #3863) or a node id that is not valid UTF-8
(it cannot name a node; refused before any read; the flag does not matter for either), 3 the row could
not be read honestly (flag ON: owner down, refusing or answering malformed; flag OFF: a store that exists but cannot be read as a
store, #3954; either flag: a row that does not fit the node schema).

Examples:
    ENGRAM_HOME=/tmp/test-engram python inspect_raw.py ob_NNNN
    python inspect_raw.py dv_NNNN --field confidence_history
    python inspect_raw.py ob_NNNN --field embedding | python -c "import json,sys; v=json.load(sys.stdin); print(f'len={len(v)}, first5={v[:5]}')"
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Locate the shared tools helper: support both "run as module" and direct invocation (same pattern as
# tools/weakest_load_bearing.py and tools/cohort_dispatch.py).
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import _owner_read as owner_read  # noqa: E402

# The ENGRAM home has ONE definition: owner_read.engram_home() (#3863). A relative value must not raise at import
# (the importing tests and `--help` still work): _get_row refuses it (live, one stderr line, exit 2) before any
# read, and until then the constants point below a file (/dev/null), where nothing can be opened or created.
try:
    DATA_DIR = owner_read.engram_home()
except ValueError:
    DATA_DIR = Path("/dev/null/engram-home-refused")
DB_PATH = DATA_DIR / "knowledge.db"

# Columns that exist as top-level fields in the nodes table.
_TABLE_STRIPPED_COLS = frozenset({
    "embedding",
    "confidence_history",
})

# Keys inside the metadata JSON blob that are stripped from MCP returns.
_METADATA_STRIPPED_KEYS = frozenset({
    "content_hash",
    "git_sha",
})

# Synthetic field assembled at runtime (not a column or metadata key).
_SYNTHETIC_FIELDS = frozenset({
    "parsed_metadata",
})

ALL_STRIPPED_FIELDS = _TABLE_STRIPPED_COLS | _METADATA_STRIPPED_KEYS | _SYNTHETIC_FIELDS


# The one statement the tool sends, registered byte for byte in src/engram/engramd_read_templates.json under
# the site tools/inspect_raw.py:read_row. The tool reads exactly: type, metadata (content_hash, git_sha,
# parsed_metadata), the stripped table columns, and id for the WHERE. The row dict is built BY POSITION from
# _ROW_COLUMNS: the owner's read connection has no row_factory, so sqlite3.Row names are never available.
_ROW_COLUMNS = ("id", "type", "embedding", "confidence_history", "metadata")
_ROW_SQL = "SELECT id, type, embedding, confidence_history, metadata FROM nodes WHERE id = ?"

OWNER_FLAG_KEY = "inspect_raw_reads_via_owner"
OWNER_READ_FAILED_EXIT = 3  # 1 means "node not found" and stays that; argparse uses 2
BAD_HOME_EXIT = 2  # a relative ENGRAM_HOME: a misconfigured call, refused before any read (argparse's code too)
BAD_ID_EXIT = BAD_HOME_EXIT  # a node id that is not valid UTF-8: the same kind of refusal (a bad call, before any read)


class InspectRawOwnerReadError(RuntimeError):
    """The owner could not give the node's row (flag ON), or gave a row that does not fit. Raised instead of
    returning None: None reads downstream as "the node is not in the store", an answer the store never gave."""


def _owner_spec() -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=InspectRawOwnerReadError,
        home=owner_read.engram_home(),
        never_empty="Not answering 'node not found': that would read as the node not existing in the store.",
    )


def _is_str_or_none(cell) -> bool:
    return cell is None or isinstance(cell, str)


def _checked_row(rows, node_id: str) -> "dict | None":
    """The one row as a dict keyed by _ROW_COLUMNS, or None for zero rows. Anything that does not fit raises
    ValueError (the caller turns it into the typed error): more than one row, a row that is not exactly
    len(_ROW_COLUMNS) cells, an id that is not the requested one, a non-str type, a non-str-or-None other cell."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError("the answer is not a list of rows")
    if len(rows) == 0:
        return None
    if len(rows) > 1:
        raise ValueError(f"{len(rows)} rows for one id")
    row = rows[0]
    if isinstance(row, (str, bytes)) or not isinstance(row, (list, tuple)) or len(row) != len(_ROW_COLUMNS):
        raise ValueError(f"the row is not {len(_ROW_COLUMNS)} cells")
    cells = dict(zip(_ROW_COLUMNS, row))
    if not isinstance(cells["id"], str) or cells["id"] != node_id:
        raise ValueError("the row's id is not the requested id")
    if not isinstance(cells["type"], str):
        raise ValueError("the row's type is not a string")
    for name in ("embedding", "confidence_history", "metadata"):
        if not _is_str_or_none(cells[name]):
            raise ValueError(f"the row's {name} is neither text nor NULL")
    return cells


def read_row(db_path, node_id: str, use_owner: "bool | None" = None) -> "dict | None":
    """The raw row of `node_id` in the store at `db_path`, read through the shared owner-read helper only.

    Behind engramd.inspect_raw_reads_via_owner (default OFF) the read goes to the engramd owner when `db_path`
    is this home's store; the owner's answer replaces the db_path.exists() precondition. Flag ON and the owner
    down, refusing or answering malformed RAISES InspectRawOwnerReadError: never None, never a partial dict,
    never a direct fallback. Flag OFF opens the store `mode=ro`. None means the store answered zero rows.

    `use_owner`: None (default) reads the flag here, once; a caller that has already decided passes that
    decision (computed by `owner_read.flag_applies`, never any other way)."""
    db_path = Path(db_path)
    spec = _owner_spec()
    if use_owner is None:
        use_owner = owner_read.flag_applies(spec, db_path)
    if not use_owner:
        # Flag OFF: classify the store path before any open (#3960). Not found stays a FileNotFoundError (exit 1
        # in the CLI); a path that cannot be checked, or is not a regular file (a FIFO would block the open), is
        # "could not be read" (exit 3), with the helper's one-line message and a remedy that is never "create it".
        # Kept: it maps the unusable classes to InspectRawOwnerReadError (exit 3); open_reader's own check would
        # raise them unmapped.
        try:
            owner_read.check_store_path(db_path)
        except owner_read.StoreUnusable as exc:
            raise InspectRawOwnerReadError(str(exc)) from exc
    # Failures from the owner-read helper (owner down, a refusal, an unusable answer, a failed ping, a client
    # that will not load) propagate UNCHANGED: each already carries its own complete remedy, and some say the
    # flag will not help. Only the malformed-row message below is this function's own, so its wording depends
    # on use_owner (flag OFF names neither the owner nor the flag).
    if use_owner:
        owner_read.ping(spec)  # the owner's answer replaces the db_path.exists() precondition
    def _unreadable(exc: sqlite3.Error) -> InspectRawOwnerReadError:
        # Flag OFF only: the store exists but sqlite cannot read it as a store (not a database, a directory,
        # no `nodes` table). Not exit 1: that code means "node or store not found". Names neither the owner nor
        # the flag, like the malformed-row message below.
        return InspectRawOwnerReadError(
            f"the store at {db_path} could not be read ({exc}); check the store, or retry if another process "
            f"holds a lock on it. {spec.never_empty}"
        )

    try:
        conn = owner_read.open_reader(spec, db_path, use_owner)
    except sqlite3.Error as exc:
        if use_owner:
            raise
        raise _unreadable(exc) from exc
    try:
        try:
            rows = conn.execute(_ROW_SQL, (node_id,)).fetchall()
        except sqlite3.Error as exc:
            if use_owner:
                raise
            raise _unreadable(exc) from exc
        try:
            return _checked_row(rows, node_id)
        except ValueError as exc:
            via = "the engramd owner answered" if use_owner else "the store answered"
            remedy = (f"retry once the owner is healthy, or set engramd.{spec.flag_key} to false to read "
                      "the store directly" if use_owner else "check the store")
            raise InspectRawOwnerReadError(
                f"{via} a row that does not match the node schema ({exc}); {remedy}. {spec.never_empty}"
            ) from exc
    finally:
        conn.close()


def _get_row(node_id: str) -> dict | None:
    # Two usage refusals, each by its own narrow check BEFORE the read, so no handler around the read has to
    # guess which of its ValueErrors it is looking at (a handler as wide as ValueError once reported a node id
    # sqlite could not encode as a relative home).
    try:
        owner_read.engram_home()  # a RELATIVE ENGRAM_HOME is refused here (alpha #3863), the one resolution for tools
    except ValueError as exc:
        print(f"ERROR: {' '.join(str(exc).split())}", file=sys.stderr)
        sys.exit(BAD_HOME_EXIT)
    try:
        node_id.encode("utf-8")  # a lone surrogate (a stray non-UTF-8 argv byte) cannot name a node: ids are UTF-8 text
    except UnicodeEncodeError:
        print(f"ERROR: node id is not valid UTF-8, so it cannot name a node: {node_id!r}", file=sys.stderr)
        sys.exit(BAD_ID_EXIT)
    try:
        return read_row(DB_PATH, node_id)
    except FileNotFoundError:
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        print(f"  Set ENGRAM_HOME to point at your engram data directory.", file=sys.stderr)
        sys.exit(1)
    except InspectRawOwnerReadError as exc:
        # Empty stdout, one stderr line (the helper's own message, verbatim), a code of its own: never 1.
        print(f"ERROR: {type(exc).__name__}: {' '.join(str(exc).split())}", file=sys.stderr)
        sys.exit(OWNER_READ_FAILED_EXIT)


def _get_field_value(row: dict, field: str):
    """Extract a stripped field from the raw DB row."""
    if field in _TABLE_STRIPPED_COLS:
        raw = row.get(field)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    if field in _METADATA_STRIPPED_KEYS:
        meta_raw = row.get("metadata") or "{}"
        try:
            meta = json.loads(meta_raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return meta.get(field)

    if field == "parsed_metadata":
        meta_raw = row.get("metadata") or "{}"
        try:
            return json.loads(meta_raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    raise ValueError(f"Unknown stripped field: {field!r}")


def _list_available(node_id: str, row: dict) -> None:
    """Print a summary of which stripped fields have non-null values for this node."""
    print(f"Node: {node_id}  (type={row.get('type', '?')})")
    print(f"DB: {DB_PATH}")
    print()
    print("Stripped field availability:")

    for field in sorted(ALL_STRIPPED_FIELDS):
        try:
            val = _get_field_value(row, field)
        except Exception as exc:
            print(f"  {field:22s}  ERROR: {exc}")
            continue

        if val is None:
            print(f"  {field:22s}  null (not set for this node)")
        elif field == "embedding":
            if isinstance(val, list):
                print(f"  {field:22s}  [{len(val)}-element float array]  first5={val[:5]}")
            else:
                print(f"  {field:22s}  {type(val).__name__} (unexpected shape)")
        elif field == "confidence_history":
            if isinstance(val, list):
                print(f"  {field:22s}  [{len(val)}-entry list]  latest={val[-1] if val else 'empty'}")
            else:
                print(f"  {field:22s}  {type(val).__name__} (unexpected shape)")
        elif field == "parsed_metadata":
            if isinstance(val, dict):
                keys = sorted(val.keys())
                print(f"  {field:22s}  dict with keys: {keys}")
            else:
                print(f"  {field:22s}  {type(val).__name__} (unexpected shape)")
        else:
            # content_hash, git_sha
            if val:
                short = str(val)[:20] + ("..." if len(str(val)) > 20 else "")
                print(f"  {field:22s}  {short!r}")
            else:
                print(f"  {field:22s}  empty string (set but empty)")

    print()
    print(f"Access a specific field: python inspect_raw.py {node_id} --field <name>")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raw access to ENGRAM fields stripped from agent-facing MCP returns.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("node_id", help="Node ID to inspect (e.g. ob_NNNN, dv_NNNN)")
    parser.add_argument(
        "--field", "-f",
        choices=sorted(ALL_STRIPPED_FIELDS),
        metavar="FIELD",
        help=f"Stripped field to retrieve. One of: {', '.join(sorted(ALL_STRIPPED_FIELDS))}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output as JSON (useful for piping). Default: human-readable.",
    )
    args = parser.parse_args()

    row = _get_row(args.node_id)
    if row is None:
        print(f"ERROR: Node '{args.node_id}' not found in {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    if args.field is None:
        _list_available(args.node_id, row)
        return

    try:
        val = _get_field_value(row, args.field)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if val is None:
        if args.as_json:
            print("null")
        else:
            print(f"Field '{args.field}' is null (not set for node {args.node_id})")
        return

    if args.as_json:
        print(json.dumps(val, indent=2))
    else:
        # Human-readable output
        if args.field == "embedding":
            if isinstance(val, list):
                print(f"embedding for {args.node_id}: [{len(val)}-element float array]")
                print(f"  first5:  {val[:5]}")
                print(f"  last5:   {val[-5:]}")
                print(f"  min={min(val):.6f}  max={max(val):.6f}")
            else:
                print(repr(val))
        elif args.field == "confidence_history":
            if isinstance(val, list):
                print(f"confidence_history for {args.node_id}: [{len(val)} entries]")
                for i, entry in enumerate(val):
                    print(f"  [{i}] {entry}")
            else:
                print(repr(val))
        elif args.field == "parsed_metadata":
            print(f"parsed_metadata for {args.node_id}:")
            print(json.dumps(val, indent=2))
        else:
            print(f"{args.field} for {args.node_id}: {val!r}")


if __name__ == "__main__":
    main()
