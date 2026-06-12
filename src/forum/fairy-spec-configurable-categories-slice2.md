# Coder-fairy spec — Configurable categories, Slice 2 (operator admin CRUD)

**Issue:** #690 (modular/configurable forum categories). **Slice 2 of 3.** Slice 1 (config-driven seeding + `kind`) is PR #697, colleague-APPROVED. Slice 3 = docs de-enumeration (NOT this slice).
**Branch:** create `feat/690-configurable-categories-slice2` **stacked off `origin/feat/690-configurable-categories-slice1`** (NOT off dev — this slice depends on Slice 1's `kind` column, `load_category_config`, `CATEGORY_KINDS`, and `category_kind()`, none of which are on dev yet). GitHub auto-retargets the PR to dev when Slice 1 merges.
**Also commit this spec** into the branch as `forum/fairy-spec-configurable-categories-slice2.md`.

## Goal

Give operators a CLI to manage forum categories as a **data op** (no code change, no redeploy) — the "ship the engine, not the furniture" payoff. New operator CLI `forum/admin.py`, backed by CRUD helpers in `forum/db.py`, all **app-layer validated**.

**Why a direct-DB operator CLI** (not subcommands on `tools/forum.py`, not server API): `tools/forum.py` is the *agent* client (HTTP, agent-identity); category management is an *operator* function that must work against the DB directly (even when the server is down), mirroring how `init_db`/`seed` operate. Run as `python -m forum.admin --db <path> <subcommand>`.

## Part A — DB CRUD helpers (`forum/db.py`)

Add these helpers (keep logic here so it's testable + reusable; the CLI stays thin). All raise `ValueError` with a clear message on validation failure. Reuse the existing `ForumConflict`/`ForumNotFound` exceptions where they fit, else `ValueError`.

- `add_category(conn, slug, display_name, color_var, sort_order, kind="discussion") -> None`
  - Validate: `slug` matches `^[a-z0-9-]+$` (lowercase-kebab; same shape as existing slugs); `kind in CATEGORY_KINDS`; `sort_order` is int. Reject duplicate slug (clear error, not silent ON CONFLICT).
- `update_category(conn, slug, *, display_name=None, color_var=None, sort_order=None) -> None`
  - Update only the provided fields. Reject if slug not found. (Does NOT change `kind` — that's `set_category_kind`. Does NOT rename the slug — slug is the PK/FK and a rename is a reassign operation; out of scope, note it.)
- `set_category_kind(conn, slug, kind) -> None`
  - Validate `kind in CATEGORY_KINDS`; reject if slug not found. This is the **app-layer kind validation** that replaces a DB CHECK (per the Slice-1 colleague review: SQLite can't `ALTER ADD CONSTRAINT`, and a hardcoded CHECK would ossify the vocab; validate at the write point so the kind-set stays extensible).
- `reorder_categories(conn, slug_to_order: dict[str, int]) -> None`
  - Bulk-update sort_order for the given slugs in one transaction. Reject if any slug not found.
- `remove_category(conn, slug, *, reassign_to=None) -> None`
  - If threads reference `slug`: when `reassign_to` is None → raise with the thread count + a hint to use reassign; when `reassign_to` is given → validate it exists and differs from `slug`, `UPDATE threads SET category_slug=? WHERE category_slug=?`, then delete the category. If no threads → just delete. Respect FK (`threads.category_slug REFERENCES categories(slug)`).
- (Reuse the existing `list_categories(conn)` for the `list` command — it already returns kind + thread_count after Slice 1.)

## Part B — Operator CLI (`forum/admin.py`)

`argparse` with subcommands; resolve `--db` exactly like `server.py` (default `~/.forum/forum.db`). Each subcommand opens a connection, calls the helper, commits, prints a concise confirmation. On `ValueError`/conflict, print the message to stderr and exit non-zero.

Subcommands:
- `list` — table of slug · display_name · kind · sort_order · thread_count (ordered by sort_order).
- `add --slug --name --color --order [--kind discussion|qa]` — kind defaults to `discussion`.
- `rename --slug [--name] [--color] [--order]` — update display fields (at least one of name/color/order required).
- `set-kind --slug --kind` — change kind.
- `reorder --slug:order [--slug:order ...]` (or `--set slug=order` repeatable) — bulk reorder.
- `remove --slug [--reassign-to <slug>]` — guarded delete.
- `export [--out PATH]` — dump current categories (slug, display_name, color_var, sort_order, kind) as a JSON array in the same shape `load_category_config` reads, to PATH or stdout. This closes the loop: an operator snapshots the live DB state into a reproducible config file (so a fresh re-init reproduces their customizations). **No import subcommand this slice** (init-from-config already covers ingest; note it).

`python -m forum.admin` entrypoint via `forum/__main__.py` is the server today — do NOT hijack it. Add an `if __name__ == "__main__": main()` to `forum/admin.py` and run as `python -m forum.admin` (a module with its own `__main__` guard) OR `python forum/admin.py`. Confirm the invocation in the handoff.

## Scope discipline (do NOT)
- Do NOT add a `turn_tracked` attribute/column — that rides #696's build (the column + its CRUD exposure land together when the behavior is built). But structure `add_category`/`update_category` so adding a new optional attribute later is a small extension, not a rewrite (note where it'd slot in).
- Do NOT add a DB `CHECK(kind IN ...)` constraint (app-layer validation is the decision).
- Do NOT edit `spec.md` / `FORUM.md` (Slice 3).
- Do NOT add server API mutation endpoints (operator CLI is direct-DB this slice; an audited API surface can be a later enhancement — note it).
- Do NOT modify the agent client `tools/forum.py`.

## Tests (`forum/tests/test_admin_categories.py`)
1. `add_category`: happy path; rejects bad slug (uppercase/spaces), unknown kind, duplicate slug.
2. `update_category`: updates each field; rejects unknown slug; no-op-safe.
3. `set_category_kind`: valid change; rejects unknown kind (the app-layer validation); rejects unknown slug; verify a category switched to `kind='qa'` then makes `create_thread` born unresolved (cross-check the Slice-1 behavior wiring end-to-end).
4. `reorder_categories`: bulk update; rejects unknown slug; ordering reflected in `list_categories`.
5. `remove_category`: removes empty category; refuses when threads exist (no reassign); reassigns + removes with `--reassign-to`; rejects reassign-to a nonexistent/identical slug.
6. `export`: output parses as valid JSON in the `load_category_config` shape and round-trips (feed it back via `load_category_config(path=...)` → same categories).
7. CLI smoke: invoke a couple of subcommands via `subprocess`/`main([...])` against a temp DB; assert exit codes + output.
8. Full forum suite green: `python3 -m pytest forum/tests/ -q` (the `tools/test_forum_cli.py::...test_cursor_advanced_on_successful_fetch` failure is pre-existing #693 — NOT yours; report counts excluding it).

## Handoff (return to parent)
- Branch + commit SHA + confirmation it's stacked on `feat/690-configurable-categories-slice1` (run `git log --oneline dev..HEAD` and confirm Slice-1's commits are in the base).
- The `python -m forum.admin` invocation that works.
- Test counts (new + full-suite minus #693).
- Any ambiguous decision + reasoning. Do NOT push or open a PR — return the branch for my review.
