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
    content_hash       SHA-256 of the source file content (file:// obs only)
    git_sha            Git commit SHA when the file was read (file:// obs only)
    parsed_metadata    Full parsed metadata dict (superset of metadata string)

Honors ENGRAM_HOME environment variable (same pattern as server.py).
Does NOT honor the #348 hardcoded-path bug — uses the env-var correctly.

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

DATA_DIR = Path(os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"))
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


def _get_row(node_id: str) -> dict | None:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        print(f"  Set ENGRAM_HOME to point at your engram data directory.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        conn.close()


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
