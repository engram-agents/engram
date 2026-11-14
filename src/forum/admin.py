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
    token issue <agent> [--write-to PATH [--force]]
                                  — mint a bearer token. With --write-to the token goes to a
                                    0600 file and stdout carries only the path and agent;
                                    without it the raw token is printed ONCE on stdout
    token count                   — number of live tokens
    token revoke <agent>          — revoke all of an agent's live tokens
    token list                    — agent, created, revoked, hash prefix (never a token)

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
import stat
import sys
from typing import Any

from .fileperms import create_if_absent
from .db import (
    CATEGORY_KINDS,
    ForumConflict,
    ForumNotFound,
    add_category,
    init_db,
    is_valid_agent_name,
    issue_token,
    list_categories,
    list_tokens,
    remove_category,
    reorder_categories,
    revoke_tokens,
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
    # Leaf only: makedirs' mode does not reach intermediates; an existing dir is never chmodded.
    os.makedirs(db_dir, mode=0o750, exist_ok=True)
    # Create forum.db 0640 ourselves: the admin CLI runs at the caller's umask, and
    # sqlite3.connect would create it at that umask (#4530, follow-up to #4499).
    # realpath: a dangling symlink counts as present to create_if_absent, so resolve it
    # and create the link's target 0640, as the export branch does.
    create_if_absent(os.path.realpath(db_path))
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
        # Absent path (or dangling symlink's target): created 0640 first. An existing
        # file is still overwritten as before, mode untouched (#4530).
        create_if_absent(os.path.realpath(out_path))
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

def _write_token_file(path: str, agent: str, conn: sqlite3.Connection, force: bool) -> int:
    """``token issue --write-to``: create ``path`` 0600 and put the token in it.

    Contract: the file is created with O_CREAT|O_EXCL at mode 0600 (an existing path
    is refused unless ``force``, which unlinks it first and still creates it fresh);
    a parent directory that is group/world-writable without the sticky bit is
    refused; every refusal happens BEFORE a token is minted; stdout carries only
    the path and the agent, never the token.
    """
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path)
    try:
        pmode = os.stat(parent).st_mode
    except OSError as exc:
        print(f"error: cannot use {parent}: {exc.strerror}", file=sys.stderr)
        return 2
    if pmode & (stat.S_IWGRP | stat.S_IWOTH) and not pmode & stat.S_ISVTX:
        print(f"error: refusing to write a token into {parent}: it is group/world-writable "
              "without the sticky bit", file=sys.stderr)
        return 2
    if not isinstance(agent, str) or "\n" in agent or not is_valid_agent_name(agent):
        print(f"error: invalid agent name {agent!r}", file=sys.stderr)
        return 2
    if os.path.lexists(path):
        if not force:
            print(f"error: {path} already exists (use --force to replace it)", file=sys.stderr)
            return 2
        try:
            os.unlink(path)
        except OSError as exc:
            print(f"error: cannot replace {path}: {exc.strerror}", file=sys.stderr)
            return 2
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        print(f"error: cannot create {path}: {exc.strerror}", file=sys.stderr)
        return 2
    try:
        raw = issue_token(conn, agent)
        with os.fdopen(fd, "w") as fh:
            fh.write(raw + "\n")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    print(f"{path} {agent}")
    return 0


def cmd_token(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """``token issue|revoke|list`` (#1459).

    ``issue`` prints the raw token as the ONLY stdout line (the note that it will
    not be shown again goes to stderr); only its sha256 is stored. ``revoke`` and
    ``list`` never print a token.
    """
    action = args.token_command
    if action == "count":
        print(sum(1 for r in list_tokens(conn) if r["revoked_at"] is None))
        return 0
    if action == "issue":
        if args.write_to:
            return _write_token_file(args.write_to, args.agent, conn, args.force)
        try:
            raw = issue_token(conn, args.agent)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(raw)
        print(f"token issued for {args.agent}; it will not be shown again "
              "(only its sha256 is stored). Warning: stdout lands in shell history and "
              "logs; prefer --write-to <path> (0600 file).", file=sys.stderr)
        return 0
    if action == "revoke":
        n = revoke_tokens(conn, args.agent)
        print(f"revoked {n} live token(s) for {args.agent}")
        return 0
    rows = list_tokens(conn)
    if not rows:
        print("(no tokens)")
        return 0
    print(f"{'AGENT':<24} {'HASH':<10} {'CREATED':<28} REVOKED")
    for r in rows:
        print(f"{r['agent']:<24} {r['hash_prefix']:<10} {r['created_at']:<28} {r['revoked_at'] or '-'}")
    return 0


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

    # token (#1459)
    p_tok = sub.add_parser("token", help="Manage per-agent bearer tokens.")
    tok_sub = p_tok.add_subparsers(dest="token_command", metavar="ACTION")
    tok_sub.required = True
    p_ti = tok_sub.add_parser("issue", help="Mint a token; the raw token is printed once.")
    p_ti.add_argument("agent")
    p_ti.add_argument("--write-to", metavar="PATH", default=None,
                      help="Create PATH (mode 0600, O_EXCL) holding the token; print only path and agent.")
    p_ti.add_argument("--force", action="store_true",
                      help="With --write-to: replace an existing file.")
    p_tr = tok_sub.add_parser("revoke", help="Revoke all of an agent's live tokens.")
    p_tr.add_argument("agent")
    tok_sub.add_parser("list", help="List tokens (hash prefix only, never the token).")
    tok_sub.add_parser("count", help="Print the number of live (unrevoked) tokens.")

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
            "token": cmd_token,
        }
        handler = dispatch[args.command]
        return handler(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
