"""The ONE definition of "not searchable" (Lei 2026-10-08, tracker #2005 section 4).

Stdlib-only on purpose, so that both ``engram_core`` (which re-exports these
names) and ``hooks/claude/engram-surface-hook.py`` (which searches foreign,
read-only attached-pack DBs and must not import ``engram_core``) can build the
rule from the same source, never from a hand-copied literal.

Node types in ``SEARCH_EXCLUDED_TYPES`` are reached by TRAVERSAL (cites edges,
engram_inspect, engram_get_subgraph, engram_list), never by SEARCH (FTS or
semantic), and they do not consume tier-2 slots -- the tier budget exists
purely for search efficiency. "Searchable" = is_current AND type NOT IN this
set. Do NOT hand-write ``type != 'evidence'`` anywhere else.
"""

from __future__ import annotations

SEARCH_EXCLUDED_TYPES: frozenset = frozenset({"evidence"})


def not_search_excluded_sql(alias: str = "") -> str:
    """SQL fragment: ``<alias>.type NOT IN (<SEARCH_EXCLUDED_TYPES>)``.

    For search sites that carry their own is_current / include_superseded
    handling and only need the type half of the rule. The values are code
    constants (never user input), so they are inlined as quoted literals.
    """
    prefix = f"{alias}." if alias else ""
    quoted = ", ".join(f"'{t}'" for t in sorted(SEARCH_EXCLUDED_TYPES))
    return f"{prefix}type NOT IN ({quoted})"


def searchable_where_sql(alias: str = "") -> str:
    """SQL fragment for the full searchable predicate: current AND not excluded."""
    prefix = f"{alias}." if alias else ""
    return f"{prefix}is_current = 1 AND {not_search_excluded_sql(alias)}"
