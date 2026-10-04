"""Shared import-or-skip for tests that need the real `src/forum/` package (#3381).

`forum.db` / `forum.server` import third-party packages listed in
`src/forum/requirements.txt` (bleach, bs4, markdown_it, flask, ...). On a
seat without them, the import must SKIP the dependent tests rather than abort
collection of the whole run.

The skip is derived from the failing import itself, never from a hard-coded
list of package names: a list fixes today's dependencies and drifts the next
time the forum gains one. Only a `ModuleNotFoundError` that names a
NON-forum module skips. A missing `forum` / `forum.*` module re-raises, and so
does any other ImportError (e.g. "cannot import name 'db' from 'forum'", the
tools/forum.py shadowing failure): `importorskip("forum.db")` would swallow
those and turn a real bug into a skip.
"""

from __future__ import annotations

import importlib

import pytest


def import_forum_or_skip(modname: str):
    """Import a forum module; SKIP only if a THIRD-PARTY dependency is missing."""
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing and missing != "forum" and not missing.startswith("forum."):
            pytest.skip(
                f"forum package dependency not installed: {missing} "
                "(src/forum/requirements.txt)",
                allow_module_level=True,  # called at module top-level by test_forum_db_hostname
            )
        raise
