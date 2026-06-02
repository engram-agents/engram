"""Operator CLI for managing forum categories as a data op.

Run as:
    python -m forum.admin --db <path> <subcommand> [args...]

or:
    python forum/admin.py --db <path> <subcommand> [args...]

This is a direct-DB operator tool — it works even when the forum server is
down, mirroring how init_db/seed operate. It is NOT the agent client
(tools/forum.py is the HTTP agent client).

Subcommands:
    list                          — table of all categories
    add --slug --name --color --order [--kind]
    rename --slug [--name] [--color] [--order]
    set-kind --slug --kind
    reorder --set SLUG=ORDER [--set SLUG=ORDER ...]
    remove --slug [--reassign-to SLUG]
    export [--out PATH]

Export note: --out writes a JSON file in the load_category_config shape,
round-trippable back through init_db. No import subcommand this slice —
init_db(conn, categories_config=path) already handles ingest from a JSON file.

API mutation endpoints (server-side REST) are intentionally not part of this
slice; they can be added as a later enhancement if audited exposure is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any

from .db import (
    CATEGORY_KINDS,
    ForumConflict,
    ForumNotFound,
    add_category,
    init_db,
    list_categories,
    remove_category,
    reorder_categories,
    set_category_kind,
    update_category,
)


# ---------------------------------------------------------------------------
# DB connection helper (matches server.py resolution pattern)
# ---------------------------------------------------------------------------

def _open_db(db_path: str) -> sqlite3.Connection:
    """Open (and init) the forum DB at db_path.

    Mirrors server.py: resolves path, ensures parent dir exists, calls init_db
    so schema migrations apply even when invoked against a fresh file.
    """
    db_path = os.path.expanduser(db_path)
    db_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def cmd_list(conn: sqlite3.Connection, _args: argparse.Namespace) -> int:
    """Print a table of all categories ordered by sort_order."""
    cats = list_categories(conn)
    if not cats:
        print("(no categories)")
        return 0

    # Fetch sort_order per slug (list_categories doesn't return it).
    order_map: dict[str, int] = {}
    for r in conn.execute("SELECT slug, sort_order FROM categories").fetchall():
        order_map[r[0]] = r[1]

    # Column widths (order width derived from the actual order_map values).
    w_slug = max(len("slug"), *(len(c["slug"]) for c in cats))
    w_name = max(len("display_name"), *(len(c["display_name"]) for c in cats))
    w_kind = max(len("kind"), *(len(c["kind"]) for c in cats))
    w_order = max(len("order"), *(len(str(v)) for v in order_map.values()))
    w_threads = len("threads")

    header = (
        f"{'slug':<{w_slug}}  "
        f"{'display_name':<{w_name}}  "
        f"{'kind':<{w_kind}}  "
        f"{'order':>{w_order}}  "
        f"{'threads':>{w_threads}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for c in cats:
        order = order_map.get(c["slug"], 0)
        print(
            f"{c['slug']:<{w_slug}}  "
            f"{c['display_name']:<{w_name}}  "
            f"{c['kind']:<{w_kind}}  "
            f"{order:>{w_order}}  "
            f"{c['thread_count']:>{w_threads}}"
        )
    return 0


def cmd_add(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Add a new category."""
    try:
        add_category(
            conn,
            slug=args.slug,
            display_name=args.name,
            color_var=args.color,
            sort_order=args.order,
            kind=args.kind,
        )
    except (ValueError, ForumConflict) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"added category {args.slug!r} (kind={args.kind!r}, order={args.order})")
    return 0


def cmd_rename(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Update display fields on an existing category."""
    if args.name is None and args.color is None and args.order is None:
        print(
            "error: rename requires at least one of --name, --color, --order",
            file=sys.stderr,
        )
        return 1
    try:
        update_category(
            conn,
            args.slug,
            display_name=args.name,
            color_var=args.color,
            sort_order=args.order,
        )
    except (ValueError, ForumNotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"updated category {args.slug!r}")
    return 0


def cmd_set_kind(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Change the kind of a category."""
    try:
        set_category_kind(conn, args.slug, args.kind)
    except (ValueError, ForumNotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"set category {args.slug!r} kind to {args.kind!r}")
    return 0


def cmd_reorder(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Bulk-reorder categories from --set SLUG=ORDER entries."""
    if not args.set:
        print("error: reorder requires at least one --set SLUG=ORDER", file=sys.stderr)
        return 1

    slug_to_order: dict[str, int] = {}
    for entry in args.set:
        if "=" not in entry:
            print(
                f"error: --set value {entry!r} must be in SLUG=ORDER format",
                file=sys.stderr,
            )
            return 1
        slug, _, raw_order = entry.partition("=")
        slug = slug.strip()
        raw_order = raw_order.strip()
        try:
            order = int(raw_order)
        except ValueError:
            print(
                f"error: order value for {slug!r} must be an integer, got {raw_order!r}",
                file=sys.stderr,
            )
            return 1
        slug_to_order[slug] = order

    try:
        reorder_categories(conn, slug_to_order)
    except (ValueError, ForumNotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"reordered {len(slug_to_order)} category/categories")
    return 0


def cmd_remove(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Remove a category, with optional thread reassignment."""
    try:
        remove_category(conn, args.slug, reassign_to=args.reassign_to)
    except (ValueError, ForumNotFound, ForumConflict) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.reassign_to:
        print(
            f"reassigned threads from {args.slug!r} to {args.reassign_to!r} "
            f"and removed {args.slug!r}"
        )
    else:
        print(f"removed category {args.slug!r}")
    return 0


def cmd_export(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Export current categories as JSON in the load_category_config shape.

    The output is round-trippable: feed it back via
    ``init_db(conn, categories_config=path)`` (or ``load_category_config(path)``)
    to reproduce the current category configuration.
    """
    # Query slug, display_name, color_var, sort_order, kind directly.
    rows = conn.execute(
        "SELECT slug, display_name, color_var, sort_order, kind "
        "FROM categories ORDER BY sort_order"
    ).fetchall()
    payload: list[dict[str, Any]] = [
        {
            "slug": r[0],
            "display_name": r[1],
            "color_var": r[2],
            "sort_order": r[3],
            "kind": r[4],
        }
        for r in rows
    ]
    out = json.dumps(payload, indent=2, ensure_ascii=False)

    if args.out:
        out_path = os.path.expanduser(args.out)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(out)
            fh.write("\n")
        print(f"exported {len(payload)} categories to {out_path!r}")
    else:
        print(out)

    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="forum.admin",
        description="Operator CLI for managing forum categories (direct-DB).",
    )
    p.add_argument(
        "--db",
        default=os.path.expanduser("~/.forum/forum.db"),
        help="Path to the forum SQLite database (default: ~/.forum/forum.db).",
    )

    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # list
    sub.add_parser("list", help="List all categories.")

    # add
    p_add = sub.add_parser("add", help="Add a new category.")
    p_add.add_argument("--slug", required=True, help="Unique slug (^[a-z0-9-]+$).")
    p_add.add_argument("--name", required=True, dest="name", help="Display name.")
    p_add.add_argument("--color", required=True, help="CSS color variable (e.g. var(--accent)).")
    p_add.add_argument("--order", required=True, type=int, help="Sort order (integer).")
    p_add.add_argument(
        "--kind",
        default="discussion",
        choices=list(CATEGORY_KINDS),
        help=f"Category kind ({', '.join(CATEGORY_KINDS)}); default: discussion.",
    )

    # rename
    p_rename = sub.add_parser("rename", help="Update display fields on a category.")
    p_rename.add_argument("--slug", required=True, help="Category slug.")
    p_rename.add_argument("--name", default=None, help="New display name.")
    p_rename.add_argument("--color", default=None, help="New CSS color variable.")
    p_rename.add_argument("--order", default=None, type=int, help="New sort order.")

    # set-kind
    p_sk = sub.add_parser("set-kind", help="Change the kind of a category.")
    p_sk.add_argument("--slug", required=True, help="Category slug.")
    p_sk.add_argument(
        "--kind",
        required=True,
        choices=list(CATEGORY_KINDS),
        help=f"New kind ({', '.join(CATEGORY_KINDS)}).",
    )

    # reorder
    p_reorder = sub.add_parser("reorder", help="Bulk-reorder categories.")
    p_reorder.add_argument(
        "--set",
        action="append",
        metavar="SLUG=ORDER",
        help="Set sort_order for SLUG (repeatable, e.g. --set cold-start=1 --set q-and-a=8).",
    )

    # remove
    p_remove = sub.add_parser("remove", help="Remove a category.")
    p_remove.add_argument("--slug", required=True, help="Category slug to remove.")
    p_remove.add_argument(
        "--reassign-to",
        default=None,
        metavar="SLUG",
        help="Reassign existing threads to this category before removal.",
    )

    # export
    p_export = sub.add_parser(
        "export",
        help="Export categories as JSON (round-trippable via load_category_config).",
    )
    p_export.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Write JSON to PATH instead of stdout.",
    )

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Parse args, dispatch subcommand, return exit code.

    Accepts an optional argv for testing (subprocess alternative).
    """
    p = build_parser()
    args = p.parse_args(argv)

    conn = _open_db(args.db)
    try:
        dispatch = {
            "list": cmd_list,
            "add": cmd_add,
            "rename": cmd_rename,
            "set-kind": cmd_set_kind,
            "reorder": cmd_reorder,
            "remove": cmd_remove,
            "export": cmd_export,
        }
        handler = dispatch[args.command]
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
