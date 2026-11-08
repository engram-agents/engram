"""Agent-name validation — the coordination-layer SSoT (#1468).

This lives in ``coordination`` (the lowest, portable layer) so the authoritative
guard can sit at the key-formation chokepoint — ``dm_thread_key`` — without an
up-import into ``forum.db``. The DM thread file for a pair is keyed ``<a>+<b>``
(sorted, ``+``-joined), so a name containing ``+`` would silently collide two
distinct pairs onto one thread file (cross-delivery). The allowlist also keeps
whitespace and path separators out of the on-disk filename.

Layering (the reason this is here and not in ``forum.db``): ``coordination`` has
zero up-imports into ``forum.*`` — it's the reusable lower layer. ``forum.db``
re-exports ``is_valid_agent_name`` from here (one regex, one definition — no
validator drift, the class #1468 was born from); the HTTP edge raises
``forum.db.ForumInvalidAgentName(ForumBadRequest)`` for its own 400-mapping, and
``dm_thread_key`` raises the coordination-native :class:`InvalidAgentName` at the
chokepoint that every caller (HTTP routes, the future ``ia dm`` thin-client,
reads and writes) passes through.
"""

from __future__ import annotations

import re

# Allowlist: lowercase alnum start, then alnum/_/- , max 63 chars. ``+`` (the
# dm_thread_key separator), whitespace, and path separators are all excluded.
# Identical regex to forum.db's (which now re-exports this) — one definition.
# Applied with ``fullmatch`` (#1459): ``$`` also matches before one trailing newline, so a
# ``match`` + ``$`` pair accepted "alice\n" -- and a name becomes a token's agent, a file name
# and an identity comparison key. ``fullmatch`` refuses it.
_AGENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


class InvalidAgentName(ValueError):
    """An agent name failed the charset allowlist (see :func:`is_valid_agent_name`).

    Coordination-native (subclasses ``ValueError``, NOT ``forum.db.ForumBadRequest``
    — coordination must not depend up on forum). Raised by ``dm_thread_key`` at the
    key-formation chokepoint. HTTP callers pre-validate via ``is_valid_agent_name``
    for a clean 400; this raise is the authoritative backstop nobody can route
    around (the ``ia dm`` CLI reaches ``dm_thread_key`` without the HTTP guard).
    """


def is_valid_agent_name(name: str) -> bool:
    """True if ``name`` is a safe agent/DM identifier (matches the allowlist)."""
    return bool(_AGENT_NAME_RE.fullmatch(name))


# ---------------------------------------------------------------------------
# Project (baton) id -- #3286
# ---------------------------------------------------------------------------

# The CLI's rule, verbatim: ``_PROJECT_ID_RE`` in ``src/engram/tools/baton.py``.
# The server cannot import the CLI, so the pattern is duplicated here and
# ``src/forum/tests/test_project_id_traversal_3286.py`` fails if the two drift.
# Always apply with ``fullmatch`` (the CLI's ``.match`` + ``$`` lets one trailing
# newline through; a project id becomes a file name here, so it must not).
PROJECT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*-[A-Za-z0-9][A-Za-z0-9_-]*$")


class InvalidProjectId(ValueError):
    """A project id that cannot safely name a baton file under the store.

    Raised by the file store's single path helper (path separator, NUL, empty,
    non-string, ``.``/``..``, or a path that resolves outside the store through a
    symlink). Routes map it to 404 (GET) or 422 (writes).
    """


class InvalidDmPath(ValueError):
    """A computed DM thread path that is not a plain file inside ``<root>/dm``.

    The target's final component is a symlink (dangling or not), or the path
    resolves outside ``<root>/dm`` (e.g. through a symlinked parent). Raised by
    the file store's DM path helper and its no-follow read helper; HTTP routes
    surface it (``GET`` and ``POST /api/dm/<counterpart>`` answer a named JSON
    500).

    It deliberately does NOT subclass ``OSError``, and that is load-bearing: both
    DM call sites carry ``except OSError:`` handlers that mean "no thread yet".
    An ``OSError``-derived refusal would be swallowed into "no thread" and the
    store would fail OPEN, which is the exact defect this type exists to prevent.
    Do not rebase it onto ``OSError``.
    """


def is_valid_project_id(project_id) -> bool:
    """True if ``project_id`` is a string matching the CLI's project-id rule."""
    return isinstance(project_id, str) and PROJECT_ID_RE.fullmatch(project_id) is not None
