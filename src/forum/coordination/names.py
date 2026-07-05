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
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


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
    return bool(_AGENT_NAME_RE.match(name))
