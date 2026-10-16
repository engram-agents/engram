"""#2348 — bind docs/cascade-machine.md's §13 "Bindable claims" prose to the
actual `CASCADE_RELATIONS` / `EDGE_CLASSIFICATIONS` / `_CASCADE_RESOLUTION_KINDS`
/ `cascade_markers` schema in src/engram/engram_core.py and
src/engram/engram_revision.py.

Why this test exists: §13 itself explains it — the doc enumerates the cascade
machine's constants by hand, in prose, and nothing mechanically binds those
claims to the code they describe. The section names
`tests/test_2252_partition_doc_binding.py` as the pattern to follow, "including
its can-it-fail control — the binding must be read from this file, not
hand-copied into the test." This file follows that discipline: every assertion
below parses docs/cascade-machine.md §13 at test time (never re-typing a
value as a Python literal), and `test_can_it_fail_control` below proves the
parsing is load-bearing by corrupting a parsed doc string and showing the
comparison then actually fails.

Import-pattern contract (#948-style): imports engram_core / engram_revision
directly and contains no ``import server``.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engram_core  # noqa: E402 — top-level import for AST-visible selection edge
import engram_revision  # noqa: E402


_DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "cascade-machine.md"

_SECTION_START_MARKER = "## 13. Bindable claims"
_SECTION_END_MARKER = "*Agent-facing procedure"


def _extract_section(doc_text):
    """Slice out §13's body (between its own heading and the closing italic
    line that follows it), so every parser below only ever searches within
    §13 itself — not the rest of the document, which could coincidentally
    contain similarly-shaped prose elsewhere.

    Fails LOUDLY (raises) if either marker is missing, same discipline as
    the reference file's `_parse_doc_type_list` — a doc restructure that
    moves or renames §13 is a signal this parser needs updating, not a
    silent empty match.
    """
    try:
        start = doc_text.index(_SECTION_START_MARKER)
        end = doc_text.index(_SECTION_END_MARKER, start)
    except ValueError as exc:
        raise AssertionError(
            f"Could not locate §13 ('{_SECTION_START_MARKER}' ... "
            f"'{_SECTION_END_MARKER}') in {_DOC_PATH} — the doc's section "
            f"structure has changed; update this file's markers to match."
        ) from exc
    return doc_text[start:end]


def _section_text():
    """Read + slice §13 lazily, INSIDE the calling test — never at module
    import. At import, any read/parse failure is a collection error that
    takes all five tests down as one opaque crash (colleague finding, PR
    #2434 review); computed per-test, a doc restructure fails exactly the
    assertions that depend on it, each with its own named message."""
    return _extract_section(_DOC_PATH.read_text())


def _parse_cascade_relations_bullet(section_text):
    """Parse the first bullet's "comprises exactly: `a`, `b`." clause.

    Terminates the capture at the ". (" that opens the bullet's own
    parenthetical about `supported_by` NOT being a member — so that
    parenthetical (which itself contains a backtick-quoted, deliberately
    non-member identifier) is never swept into the parsed membership set.
    Fails loudly if the expected shape isn't found.
    """
    m = re.search(r"comprises exactly:(.*?)\.\s*\(", section_text, re.DOTALL)
    if not m:
        raise AssertionError(
            "Could not find the 'CASCADE_RELATIONS ... comprises exactly: "
            "`a`, `b`. (`supported_by` is NOT a member ...)' bullet shape in "
            "§13 of docs/cascade-machine.md — update "
            "_parse_cascade_relations_bullet to match the doc's current "
            "prose."
        )
    members = frozenset(re.findall(r"`([a-zA-Z_][a-zA-Z0-9_-]*)`", m.group(1)))
    # The bullet's own parenthetical names `supported_by` as an explicit
    # NON-member. Assert our capture window genuinely excluded it, rather
    # than trusting the regex boundary silently — this is the exact sweep
    # failure mode the spec calls out by name.
    assert "supported_by" not in members, (
        "the CASCADE_RELATIONS bullet parser swept `supported_by` in from "
        "its own parenthetical non-member note — the capture boundary "
        "(the '. (' terminator) is not doing its job; fix "
        "_parse_cascade_relations_bullet before trusting its output"
    )
    return members


def _parse_resolution_kinds_bullet(section_text):
    """Parse the second bullet's "resolution kinds are exactly: `a`, `b`,
    `c` (`_CASCADE_RESOLUTION_KINDS`)." clause, excluding the trailing
    backtick-quoted constant-name parenthetical from the captured set."""
    m = re.search(
        r"resolution kinds are exactly:(.*?)\(`_CASCADE_RESOLUTION_KINDS`\)",
        section_text,
        re.DOTALL,
    )
    if not m:
        raise AssertionError(
            "Could not find the 'resolution kinds are exactly: `a`, `b`, "
            "`c` (`_CASCADE_RESOLUTION_KINDS`)' bullet shape in §13 of "
            "docs/cascade-machine.md — update _parse_resolution_kinds_bullet "
            "to match the doc's current prose."
        )
    return frozenset(re.findall(r"`([a-zA-Z_][a-zA-Z0-9_-]*)`", m.group(1)))


def _parse_acknowledged_derives_from_flags(section_text):
    """Parse the third bullet's "`acknowledged_derives_from` carries
    `cascade: True`, `temporal_causal: False`, `dag_family: grounding` in
    `EDGE_CLASSIFICATIONS`." clause into {flag: bool | str} (#3210: the
    bool `dag` became the string `dag_family`)."""
    m = re.search(
        r"`acknowledged_derives_from` carries(.*?)in `EDGE_CLASSIFICATIONS`\.",
        section_text,
        re.DOTALL,
    )
    if not m:
        raise AssertionError(
            "Could not find the '`acknowledged_derives_from` carries "
            "`cascade: True`, `temporal_causal: False`, `dag_family: grounding` in "
            "`EDGE_CLASSIFICATIONS`.' bullet shape in §13 of "
            "docs/cascade-machine.md — update "
            "_parse_acknowledged_derives_from_flags to match the doc's "
            "current prose."
        )
    pairs = re.findall(
        r"`(cascade|temporal_causal|dag_family): (True|False|grounding|abstraction)`",
        m.group(1),
    )
    if len(pairs) != 3:
        raise AssertionError(
            f"Expected exactly 3 flag:value pairs (cascade, temporal_causal, "
            f"dag_family) in the acknowledged_derives_from bullet, found "
            f"{len(pairs)}: {pairs} — the doc's bullet prose has changed "
            f"shape; update _parse_acknowledged_derives_from_flags."
        )
    return {
        name: (value if name == "dag_family" else value == "True")
        for name, value in pairs
    }


def _parse_marker_status_bullet(section_text):
    """Parse the fourth bullet's "marker status domain is exactly `pending`,
    `resolved`, and the `cascade_markers` schema carries no severity
    column." clause. Returns (frozenset(status_values), no_severity_claimed)."""
    m = re.search(
        r"marker status domain is exactly(.*?)and the\s*\n\s*"
        r"`cascade_markers` schema carries no severity column\.",
        section_text,
        re.DOTALL,
    )
    if not m:
        raise AssertionError(
            "Could not find the 'marker status domain is exactly `pending`, "
            "`resolved`, and the `cascade_markers` schema carries no "
            "severity column.' bullet shape in §13 of "
            "docs/cascade-machine.md — update _parse_marker_status_bullet "
            "to match the doc's current prose."
        )
    statuses = frozenset(re.findall(r"`([a-zA-Z_][a-zA-Z0-9_-]*)`", m.group(1)))
    return statuses, True  # match itself proves the "no severity" clause is present




def test_cascade_relations_matches_doc_and_derivation():
    """§13 bullet 1: the doc's claimed CASCADE_RELATIONS membership
    (`derives_from`, `acknowledged_derives_from` — NOT `supported_by`, named
    explicitly in the bullet's own parenthetical as a non-member) equals
    engram_core.CASCADE_RELATIONS, and CASCADE_RELATIONS itself equals its
    own derivation from EDGE_CLASSIFICATIONS's cascade flag."""
    DOC_CASCADE_RELATIONS = _parse_cascade_relations_bullet(_section_text())
    assert engram_core.CASCADE_RELATIONS == DOC_CASCADE_RELATIONS, (
        "engram_core.CASCADE_RELATIONS "
        f"({sorted(engram_core.CASCADE_RELATIONS)}) no longer matches "
        "docs/cascade-machine.md §13's claimed membership "
        f"({sorted(DOC_CASCADE_RELATIONS)}). If this is a deliberate "
        "cascade-flag change, update the doc's bullet AND this test "
        "together; otherwise the doc has drifted and needs correcting."
    )
    derived = frozenset(
        r for r, c in engram_core.EDGE_CLASSIFICATIONS.items() if c["cascade"]
    )
    assert engram_core.CASCADE_RELATIONS == derived, (
        "engram_core.CASCADE_RELATIONS "
        f"({sorted(engram_core.CASCADE_RELATIONS)}) no longer equals "
        "{r for r, c in EDGE_CLASSIFICATIONS.items() if c['cascade']} "
        f"({sorted(derived)}) — CASCADE_RELATIONS itself is miswired "
        "relative to EDGE_CLASSIFICATIONS."
    )


def test_resolution_kinds_matches_doc():
    """§13 bullet 2: the doc's claimed resolution kinds equal
    set(engram_revision._CASCADE_RESOLUTION_KINDS)."""
    DOC_RESOLUTION_KINDS = _parse_resolution_kinds_bullet(_section_text())
    assert DOC_RESOLUTION_KINDS == set(engram_revision._CASCADE_RESOLUTION_KINDS), (
        "docs/cascade-machine.md §13's claimed resolution kinds "
        f"({sorted(DOC_RESOLUTION_KINDS)}) no longer match "
        "engram_revision._CASCADE_RESOLUTION_KINDS "
        f"({sorted(engram_revision._CASCADE_RESOLUTION_KINDS)}). Update the "
        "doc's bullet to match — unless this is a deliberate resolution-kind "
        "change, in which case update the doc AND this test together."
    )


def test_acknowledged_derives_from_flags_match_doc():
    """§13 bullet 3: the doc's claimed flags for acknowledged_derives_from
    (cascade True, temporal_causal False, dag_family grounding) equal
    EDGE_CLASSIFICATIONS["acknowledged_derives_from"] on those three keys."""
    DOC_ACK_DERIVES_FROM_FLAGS = _parse_acknowledged_derives_from_flags(
        _section_text()
    )
    code_flags = engram_core.EDGE_CLASSIFICATIONS["acknowledged_derives_from"]
    mismatches = [
        f"{key}: doc says {DOC_ACK_DERIVES_FROM_FLAGS[key]}, "
        f"EDGE_CLASSIFICATIONS says {code_flags[key]}"
        for key in ("cascade", "temporal_causal", "dag_family")
        if DOC_ACK_DERIVES_FROM_FLAGS[key] != code_flags[key]
    ]
    assert not mismatches, (
        "docs/cascade-machine.md §13's claimed acknowledged_derives_from "
        f"flags disagree with engram_core.EDGE_CLASSIFICATIONS on "
        f"{len(mismatches)} key(s):\n"
        + "\n".join(f"  - {m}" for m in mismatches)
        + "\nUpdate the doc's bullet to match the shipped declaration — "
        "unless this is a deliberate flag change, in which case update "
        "EDGE_CLASSIFICATIONS's comment, the doc's bullet, AND this test "
        "together."
    )


def _parse_cascade_markers_status_check(source_text):
    """Parse the `status` column's CHECK constraint values out of the
    cascade_markers CREATE TABLE DDL in engram_core.py's _init_db, and
    report whether a `severity` column is present anywhere in that same
    table definition.

    Located by table name (searches for the `CREATE TABLE IF NOT EXISTS
    cascade_markers (` opener and its matching closing `);`), not by a
    fixed line-number slice — so this stays correct if the table's position
    in the file shifts, matching the doc's own §13 framing of "engram_core.py
    ~lines 2364-2397" as an approximate pointer, not a load-bearing offset.
    """
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS cascade_markers \((.*?)\n\s*\);",
        source_text,
        re.DOTALL,
    )
    if not m:
        raise AssertionError(
            "Could not find the 'CREATE TABLE IF NOT EXISTS cascade_markers "
            "( ... );' DDL block in src/engram/engram_core.py — the schema "
            "definition has moved or changed shape; update "
            "_parse_cascade_markers_status_check to match."
        )
    ddl_body = m.group(1)
    status_m = re.search(
        r"status\s+TEXT NOT NULL DEFAULT 'pending' CHECK \(status IN \(([^)]*)\)\)",
        ddl_body,
    )
    if not status_m:
        raise AssertionError(
            "Could not find the 'status TEXT NOT NULL DEFAULT ... CHECK "
            "(status IN (...))' column definition inside cascade_markers' "
            "DDL — the status column's shape has changed; update "
            "_parse_cascade_markers_status_check to match."
        )
    statuses = frozenset(re.findall(r"'([a-zA-Z_]+)'", status_m.group(1)))
    has_severity_column = bool(re.search(r"\bseverity\b", ddl_body, re.IGNORECASE))
    return statuses, has_severity_column


_CORE_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "engram" / "engram_core.py"
)

def _code_marker_statuses():
    # Read lazily too — same collection-error rationale as _section_text().
    return _parse_cascade_markers_status_check(_CORE_SOURCE_PATH.read_text())


def test_marker_status_domain_matches_doc_and_no_severity_column():
    """§13 bullet 4: the marker status domain is exactly `pending`,
    `resolved`, parsed from the cascade_markers DDL's `status` CHECK
    constraint in engram_core.py, and no `severity` column exists anywhere
    in that table's DDL — both compared against the doc's claim."""
    DOC_MARKER_STATUSES, _ = _parse_marker_status_bullet(_section_text())
    CODE_MARKER_STATUSES, CODE_HAS_SEVERITY_COLUMN = _code_marker_statuses()
    assert DOC_MARKER_STATUSES == CODE_MARKER_STATUSES, (
        "docs/cascade-machine.md §13's claimed marker status domain "
        f"({sorted(DOC_MARKER_STATUSES)}) no longer matches the "
        f"cascade_markers DDL's status CHECK constraint "
        f"({sorted(CODE_MARKER_STATUSES)}) in src/engram/engram_core.py. "
        "Update the doc's bullet to match — unless this is a deliberate "
        "status-domain change, in which case update the DDL, the doc, AND "
        "this test together."
    )
    assert not CODE_HAS_SEVERITY_COLUMN, (
        "src/engram/engram_core.py's cascade_markers DDL now has a "
        "'severity'-named column — docs/cascade-machine.md §13 explicitly "
        "claims 'the cascade_markers schema carries no severity column'; "
        "either the doc's claim is now stale (update it) or the column "
        "addition should be reverted."
    )


def test_can_it_fail_control():
    """Proves the parsing above is load-bearing, per §13's own instruction
    to follow test_2252_partition_doc_binding.py's can-it-fail control: a
    doc-only mutation (source constants left untouched) must flip a
    comparison this file makes from pass to fail. If this test itself
    passes trivially (the mutated comparison still reads as equal), the
    parsers above are not actually reading the doc's content and this
    whole file is providing false assurance — exactly the failure mode
    #2277 (the referenced pattern's origin issue) exists to catch."""
    # Corrupt a copy of §13's text the same way #2299's real incident did:
    # swap in a wrong membership list for CASCADE_RELATIONS (add a
    # non-member, drop a real member) and reparse it — this must produce a
    # set that disagrees with the real engram_core.CASCADE_RELATIONS.
    section_text = _section_text()
    mutated_section = section_text.replace(
        "comprises exactly: `derives_from`,\n  `acknowledged_derives_from`.",
        "comprises exactly: `derives_from`,\n  `mutated_fake_relation`.",
        1,
    )
    assert mutated_section != section_text, (
        "the mutation string didn't match anything in §13's actual text — "
        "the bullet's prose has changed shape since this control was "
        "written; update the mutation target in test_can_it_fail_control"
    )
    mutated_relations = _parse_cascade_relations_bullet(mutated_section)
    assert mutated_relations != engram_core.CASCADE_RELATIONS, (
        "mutating §13's CASCADE_RELATIONS bullet (swapping in a wrong "
        "member) did NOT change the parsed result relative to "
        "engram_core.CASCADE_RELATIONS — the parser in this file is not "
        "actually reading the doc's content, which is exactly the failure "
        "mode this can-it-fail control exists to catch (see "
        "test_2252_partition_doc_binding.py's own 'Round-1 correction' "
        "docstring for the incident this discipline responds to)"
    )

    # Second, independent mutation: corrupt the resolution-kinds bullet by
    # dropping a real member, and confirm the parsed set no longer equals
    # engram_revision._CASCADE_RESOLUTION_KINDS.
    mutated_section_2 = section_text.replace(
        "resolution kinds are exactly: `acknowledged`, `retracted`,\n"
        "  `superseded` (`_CASCADE_RESOLUTION_KINDS`).",
        "resolution kinds are exactly: `acknowledged`, `retracted`\n"
        "  (`_CASCADE_RESOLUTION_KINDS`).",
        1,
    )
    assert mutated_section_2 != section_text, (
        "the resolution-kinds mutation string didn't match anything in "
        "§13's actual text — the bullet's prose has changed shape since "
        "this control was written; update the mutation target in "
        "test_can_it_fail_control"
    )
    mutated_kinds = _parse_resolution_kinds_bullet(mutated_section_2)
    assert mutated_kinds != set(engram_revision._CASCADE_RESOLUTION_KINDS), (
        "mutating §13's resolution-kinds bullet (dropping 'superseded') "
        "did NOT change the parsed result relative to "
        "engram_revision._CASCADE_RESOLUTION_KINDS — the parser is not "
        "actually reading the doc's content"
    )
