"""#3229 — cascade-walk prose must not enumerate a stale relation set.

`_walk_cascade_downstream` (and the supersede/retract impl docstrings that
describe the same walk) once said the cascade follows "today just
derives_from" while `engram_core.CASCADE_RELATIONS` also holds
acknowledged_derives_from. Guard: each docstring must name the
constant `CASCADE_RELATIONS` (the source of truth) and, if it lists members as
an example, list every current member. A relation added to the constant later then
fails here instead of leaving stale prose. (Naming the constant alone is not
sufficient: the stale prose already named it while listing one member.)
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engram_core  # noqa: E402
import engram_revision  # noqa: E402

_FUNCS = ["_walk_cascade_downstream", "_supersede_impl", "_retract_impl"]


def _relation_mentioned(relation, doc):
    """Whole-word match: `derives_from` must not match inside
    `acknowledged_derives_from`."""
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(relation)}(?![A-Za-z0-9_])", doc
    ) is not None


def _partial_enumeration(doc, members):
    """Return (mentioned, missing) member lists for a docstring."""
    mentioned = [r for r in members if _relation_mentioned(r, doc)]
    missing = [r for r in members if not _relation_mentioned(r, doc)]
    return mentioned, missing


def test_checker_rejects_substring_only_mention():
    """`derives_from` is a substring of `acknowledged_derives_from`; a doc
    that names only the latter must be reported as missing `derives_from`."""
    doc = "follows CASCADE_RELATIONS, today just acknowledged_derives_from"
    mentioned, missing = _partial_enumeration(
        doc, sorted(engram_core.CASCADE_RELATIONS)
    )
    assert mentioned == ["acknowledged_derives_from"]
    assert "derives_from" in missing


@pytest.mark.parametrize("name", _FUNCS)
def test_docstring_names_constant_and_enumerates_all_or_none(name):
    """Names CASCADE_RELATIONS itself, and if it enumerates any member as an
    example it enumerates every member (a partial list is the stale claim).
    Naming the constant alone is not enough: the stale prose already did."""
    doc = getattr(engram_revision, name).__doc__ or ""
    assert "CASCADE_RELATIONS" in doc, f"{name} docstring must name CASCADE_RELATIONS"
    mentioned, missing = _partial_enumeration(
        doc, sorted(engram_core.CASCADE_RELATIONS)
    )
    assert not mentioned or not missing, (
        f"{name} docstring enumerates {mentioned} but omits {missing}"
    )


def test_constant_still_has_acknowledged_derives_from():
    # Anchors the premise of #3229; if this changes, revisit the prose.
    assert "acknowledged_derives_from" in engram_core.CASCADE_RELATIONS
