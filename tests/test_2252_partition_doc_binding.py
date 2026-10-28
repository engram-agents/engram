"""#2277 — bind docs/internal/specs/2252-cb-ncb-partition.md's enumerated node-type
membership claims to the actual `CLAIM_BEARING_TYPES` / `VALID_NODE_TYPES`
constants in src/engram/engram_core.py.

Why this test exists: the CB/NCB partition ratification doc
(docs/internal/specs/2252-cb-ncb-partition.md, #2252's W0 workstream) enumerates its
membership lists BY HAND, in prose. Nothing mechanically binds those prose
claims to the constants they describe, so when the underlying source
changes, the doc can silently drift with no test signal. This already
happened once during the doc's own review round: a concurrent merge (#2263)
dropped `lesson` from `CLAIM_BEARING_TYPES` while the doc's membership
section still listed it as CB — caught only by a colleague reading
carefully, not by any test. This file is the mechanical signal that
incident argues for.

**Round-1 correction (colleague review, PR #2293):** the first version of
this file compared the constants against a hand-transcribed Python literal
copied FROM the doc — it never actually read the doc file. That binds
constants-to-test-literal, not constants-to-doc: a can-it-fail control
(corrupting the doc's prose alone, constants untouched) stayed green,
proving the doc itself could drift silently with this test providing false
assurance — exactly the direction #2277 exists to catch. Fixed by parsing
the doc's own "CB types (N...)"/"NCB types (N)" sentences at test time
(`_parse_doc_type_list` below) instead of hardcoding a transcription, so a
doc-only edit now changes what the assertions compare against, for real.

What each test would catch, if the source drifts out from under the doc
without the doc being updated to match (or vice versa — see the correction
above):

1. `test_cb_types_equals_doc_enumeration` — a CB type is added to or removed
   from `CLAIM_BEARING_TYPES` (e.g. #2263's `lesson` removal, or a future
   deliberate addition) without the doc's "CB types (6, current)" line being
   updated to match, OR the doc's line is edited without the constant
   changing. When this fails, read which side moved before assuming "update
   the doc" — a genuine constant change needs the doc updated to match; a
   doc-only edit (typo, drift, vandalism) needs the doc reverted.

2. `test_trust_signal_still_missing_from_valid_node_types` — a regression
   guard for the doc's own load-bearing caveat: `trust_signal` is a real NCB
   type that is NOT in `VALID_NODE_TYPES` at all (a separate, already-tracked
   gap, issue #2203), so a naive "NCB = VALID_NODE_TYPES minus
   CLAIM_BEARING_TYPES" derivation misses it entirely — which is exactly how
   an earlier draft of the doc got it wrong. If #2203 closes and
   `trust_signal` is added to `VALID_NODE_TYPES`, this assertion goes red as
   designed, signalling that both this file and the doc's "trust_signal is
   NOT in VALID_NODE_TYPES" claim need updating together — not that this
   assertion should just be deleted.

3. `test_ncb_types_equals_doc_enumeration_not_naive_derivation` — the doc's
   named 13-member NCB list stops matching
   `(VALID_NODE_TYPES - CLAIM_BEARING_TYPES) | {"trust_signal"}` — i.e. either
   the doc's list or the underlying constants moved without the other being
   updated to match.

4. `test_valid_node_types_fully_partitioned` — some type in
   `VALID_NODE_TYPES` falls into neither the CB set nor the doc's NCB set (or
   into both) — i.e. the doc's two lists together stop being a full, disjoint
   partition of the actual type universe. Its one non-redundant assertion
   relative to tests 1-3 is `CLAIM_BEARING_TYPES.isdisjoint(doc's NCB list)`
   (a type claimed as both CB and NCB) — the union-equality half is already
   implied by tests 1 and 3 together, kept here as explicit defense-in-depth
   rather than relied on as inference (Borges, PR #2293 colleague review).

**Edge-partition table binding, added issue #2299:** the exclusion this
docstring originally stated here — `EDGE_CLASSIFICATIONS` cascade/provenance
values were "a known, imminent, in-flight change (#2252 Stage 1.3 / PR
#2289)" and binding them "would make this test go red on the very next
unrelated merge" — was correct when #2277 was built and has since expired:
#2289 merged, `supported_by`'s `cascade` flipped to `False` exactly as
anticipated, and the doc's own edge-partition table (and its now-stale
caveat paragraph) went briefly wrong-and-confident in the gap before anyone
checked — which is #2299 itself, filed by Kepler after Borges caught it live
on `dev`. That is the exact failure this exclusion's own point (a
doc-binding test that declines to bind the one value that later goes false)
predicted about itself, and #2299's own body names it explicitly: *"the one
value the test declined to bind is exactly the value that is now false, and
the test cannot report it."* The in-flight change is no longer in flight, so
the exclusion's justification is gone — `_parse_doc_edge_table` below now
binds the doc's "## The edge partition" table (all four boolean columns:
cascade, provenance, temporal_causal, removable) to `EDGE_CLASSIFICATIONS`
directly, the same reads-the-actual-doc discipline as the membership parser
above. `CASCADE_RELATIONS` / `PROVENANCE_RELATIONS` still have their own,
separate derivation-correctness binding in tests/test_cascade_derived_sets.py
— that file checks the constants are *wired right*; this one checks the doc
*describes them right*, which is a different question the other file cannot
answer (it never reads the doc).

Import-pattern contract (#948-style): imports engram_core directly and
contains no ``import server``.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engram_core  # noqa: E402 — top-level import for AST-visible selection edge


# ---------------------------------------------------------------------------
# Parse the doc's own enumerated lists (docs/internal/specs/2252-cb-ncb-partition.md,
# "## Membership" section) at test time, rather than pinning a hand-
# transcribed Python literal — see the module docstring's "Round-1
# correction" for why this is load-bearing, not a style preference.
# ---------------------------------------------------------------------------

_DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "internal" / "specs" / "2252-cb-ncb-partition.md"


def _parse_doc_type_list(doc_text, label):
    """Parse a "**{label} types (N...):** `a`, `b`, ... `z`." sentence out of
    the doc's prose and return (frozenset(types), claimed_count).

    Regex-based against this doc's specific, stable sentence shape — not a
    general markdown parser. Deliberately fails LOUDLY (raises, rather than
    returning an empty/wrong set) if the doc's prose no longer matches the
    expected shape, since a shape change is itself a signal this parser
    needs updating alongside the doc, not a silent pass-through.

    Non-greedy match up to the first ". " followed by a blank line — the
    doc's own paragraph boundary — so this does not run on into the next
    paragraph even when that paragraph also contains backtick-quoted type
    names and periods (both the CB and NCB sentences are immediately
    followed by a paragraph that does exactly this).
    """
    m = re.search(
        rf"\*\*{re.escape(label)} types \((\d+)[^)]*\):\*\*(.*?)\.\s*\n\s*\n",
        doc_text,
        re.DOTALL,
    )
    if not m:
        raise AssertionError(
            f"Could not find a '**{label} types (N...):** ... .' sentence in "
            f"{_DOC_PATH} (searched for the exact prose shape this parser "
            f"expects) — the doc's membership section has changed shape; "
            f"update _parse_doc_type_list in this file to match."
        )
    claimed_count = int(m.group(1))
    body = m.group(2)
    # Identifier-ish, not just lowercase-and-underscore: a future type name
    # could plausibly carry a digit or hyphen, and the original [a-z_]+
    # pattern would silently DROP such a token from the parsed set rather
    # than raise — the same failure class (looks like it caught the drift,
    # actually stayed quiet) as the blocker this parser exists to fix, just
    # routed through an untested character class (round-2 reviewer-fairy,
    # PR #2293).
    types = frozenset(re.findall(r"`([a-zA-Z_][a-zA-Z0-9_-]*)`", body))
    if len(types) != claimed_count:
        raise AssertionError(
            f"docs/internal/specs/2252-cb-ncb-partition.md's own '{label} types "
            f"({claimed_count}...)' parenthetical claims {claimed_count} "
            f"members, but {len(types)} backtick-quoted type names were "
            f"parsed from that sentence: {sorted(types)}. The doc's count "
            f"and its own list disagree with each other — fix the doc "
            f"before trusting either number."
        )
    return types, claimed_count


_DOC_TEXT = _DOC_PATH.read_text()
DOC_CB_TYPES, _DOC_CB_CLAIMED_COUNT = _parse_doc_type_list(_DOC_TEXT, "CB")
DOC_NCB_TYPES, _DOC_NCB_CLAIMED_COUNT = _parse_doc_type_list(_DOC_TEXT, "NCB")


def test_cb_types_equals_doc_enumeration():
    """Pins docs/internal/specs/2252-cb-ncb-partition.md's "CB types (6, current)"
    line to engram_core.CLAIM_BEARING_TYPES.

    If this fails, the fix is almost always "update the doc to match the new
    membership" — UNLESS the change is a deliberate new/removed CB type, in
    which case the constant, this test, AND the doc change together.
    """
    assert engram_core.CLAIM_BEARING_TYPES == DOC_CB_TYPES, (
        "engram_core.CLAIM_BEARING_TYPES "
        f"({sorted(engram_core.CLAIM_BEARING_TYPES)}) no longer matches "
        "docs/internal/specs/2252-cb-ncb-partition.md's 'CB types (6, current)' "
        f"enumeration ({sorted(DOC_CB_TYPES)}). Update the doc's membership "
        "section to match — unless this is a deliberate CB-type addition or "
        "removal, in which case update the doc AND this test together."
    )


def test_trust_signal_still_missing_from_valid_node_types():
    """Regression guard for issue #2203's gap, which
    docs/internal/specs/2252-cb-ncb-partition.md's membership section leans on
    explicitly: `trust_signal` is a real NCB type that does NOT appear in
    `VALID_NODE_TYPES` at all, so a naive 'NCB = VALID_NODE_TYPES minus
    CLAIM_BEARING_TYPES' derivation misses it entirely (exactly the mistake
    an earlier draft of the doc made).

    If this assertion fails, #2203's gap has closed — flip this assertion
    (don't delete it) and update
    docs/internal/specs/2252-cb-ncb-partition.md's 'trust_signal is NOT in
    VALID_NODE_TYPES' claim to match.
    """
    assert "trust_signal" not in engram_core.VALID_NODE_TYPES, (
        "trust_signal now appears in VALID_NODE_TYPES — issue #2203's gap "
        "may have closed; docs/internal/specs/2252-cb-ncb-partition.md's 'trust_signal "
        "is NOT in VALID_NODE_TYPES' claim and this test both need updating"
    )


def test_ncb_types_equals_doc_enumeration_not_naive_derivation():
    """Pins docs/internal/specs/2252-cb-ncb-partition.md's "NCB types (13)" line to
    (VALID_NODE_TYPES - CLAIM_BEARING_TYPES) | {"trust_signal"} — NOT to a
    naive VALID_NODE_TYPES - CLAIM_BEARING_TYPES derivation, which the doc
    itself warns misses trust_signal entirely (see the test above)."""
    derived_ncb = (
        engram_core.VALID_NODE_TYPES - engram_core.CLAIM_BEARING_TYPES
    ) | {"trust_signal"}
    assert DOC_NCB_TYPES == derived_ncb, (
        "docs/internal/specs/2252-cb-ncb-partition.md's 'NCB types (13)' enumeration "
        f"({sorted(DOC_NCB_TYPES)}) no longer equals "
        "(VALID_NODE_TYPES - CLAIM_BEARING_TYPES) | {'trust_signal'} "
        f"({sorted(derived_ncb)}). Update the doc's membership section to "
        "match the current constants — unless this is a deliberate NCB "
        "membership change, in which case update the doc AND this test "
        "together."
    )
    assert len(DOC_NCB_TYPES) == 13, (
        "docs/internal/specs/2252-cb-ncb-partition.md's own 'NCB types (13)' count "
        f"claim no longer matches this test's pinned list ({len(DOC_NCB_TYPES)} "
        "members) — the doc's count needs updating"
    )


def test_valid_node_types_fully_partitioned():
    """Every member of VALID_NODE_TYPES falls into exactly one of
    CLAIM_BEARING_TYPES or the doc's NCB list (minus trust_signal, which is
    NOT a VALID_NODE_TYPES member — see the regression guard above) — no
    type falls through the cracks uncategorized, and none is claimed by
    both."""
    assert engram_core.VALID_NODE_TYPES == engram_core.CLAIM_BEARING_TYPES | (
        DOC_NCB_TYPES - {"trust_signal"}
    ), (
        "engram_core.VALID_NODE_TYPES "
        f"({sorted(engram_core.VALID_NODE_TYPES)}) is no longer fully "
        "partitioned by CLAIM_BEARING_TYPES union "
        "docs/internal/specs/2252-cb-ncb-partition.md's NCB list (minus trust_signal) "
        f"({sorted(engram_core.CLAIM_BEARING_TYPES | (DOC_NCB_TYPES - {'trust_signal'}))}) "
        "— some node type is uncategorized by the doc's membership lists"
    )
    assert engram_core.CLAIM_BEARING_TYPES.isdisjoint(DOC_NCB_TYPES), (
        "engram_core.CLAIM_BEARING_TYPES and "
        "docs/internal/specs/2252-cb-ncb-partition.md's NCB list overlap — a node "
        "type is claimed as both CB and NCB, which violates the doc's own "
        "partition definition"
    )


# ---------------------------------------------------------------------------
# Edge-partition table binding (issue #2299) — parse the "## The edge
# partition" markdown table at test time and bind its four boolean columns
# to EDGE_CLASSIFICATIONS, the same discipline as the type-list parser above.
# ---------------------------------------------------------------------------

_EDGE_TABLE_ROW_RE = re.compile(
    r"^\|\s*`([a-zA-Z_]+)`\s*\|"          # relation, backtick-quoted
    r"\s*\**(True|False)\**\s*\|"          # cascade — optionally **bold**
    r"\s*\**(True|False)\**\s*\|"          # provenance — optionally **bold**
    r"\s*\**(True|False)\**\s*\|"          # temporal_causal
    r"\s*\**(True|False)\**\s*\|",         # removable
    re.MULTILINE,
)


def _parse_doc_edge_table(doc_text):
    """Parse docs/internal/specs/2252-cb-ncb-partition.md's "## The edge partition"
    table into {relation: {"cascade": bool, "provenance": bool,
    "temporal_causal": bool, "removable": bool}}.

    Matched by the row shape itself (a line starting "| `relation` | ..."), not by
    slicing between section headers — every data row in this table starts
    with a backtick-quoted relation name immediately after the leading
    `| `, and nothing else in the doc does (checked: this is the ONLY
    matching line shape in the file, verified when this parser was written
    — issue #2299). A markdown bold marker (`**True**`/`**False**`) is
    stripped, not required: some cells are emphasized (the values Stage 1.3
    changed) and some aren't, and the boolean the cell states is the same
    either way.

    Deliberately fails LOUDLY (raises) rather than returning an empty/wrong
    dict if zero rows match — a shape change here is a signal this parser
    needs updating alongside the doc, not a silent pass-through, same
    discipline as `_parse_doc_type_list` above.

    The row regex above binds cells to flag names by POSITION (1st boolean
    cell = cascade, 2nd = provenance, ...), not by reading a column label —
    so a doc edit that reordered the table's columns (e.g. swapping
    `provenance` and `temporal_causal`) would silently mis-bind every row's
    values to the wrong flag name without the "zero rows match" guard above
    ever firing (the row shape still matches; only the meaning of each
    position changed). Guarded explicitly here (round-1 reviewer-fairy
    finding, PR #2301) by checking the table's own header row states the
    column order this parser assumes, before trusting any row.
    """
    header_match = re.search(
        r"^\|\s*relation\s*\|\s*cascade\s*\|\s*provenance\s*\|\s*temporal_causal\s*\|"
        r"\s*removable\s*\|",
        doc_text,
        re.MULTILINE,
    )
    if not header_match:
        raise AssertionError(
            f"Could not find the edge-partition table's header row "
            f"('| relation | cascade | provenance | temporal_causal | removable "
            f"| ...') in {_DOC_PATH} in that exact column order — "
            f"_parse_doc_edge_table's row regex binds cells to flag names "
            f"by position, so either the header changed order or this "
            f"parser is being run against the wrong section; update "
            f"_parse_doc_edge_table's regex to match the doc's actual "
            f"column order before trusting any parsed row."
        )
    rows = {}
    for line in doc_text.splitlines():
        m = _EDGE_TABLE_ROW_RE.match(line)
        if not m:
            continue
        relation, cascade, provenance, temporal_causal, removable = m.groups()
        rows[relation] = {
            "cascade": cascade == "True",
            "provenance": provenance == "True",
            "temporal_causal": temporal_causal == "True",
            "removable": removable == "True",
        }
    if not rows:
        raise AssertionError(
            f"Could not find any '| `relation` | True/False | ... |' table "
            f"rows in {_DOC_PATH} — the doc's edge-partition table has "
            f"changed shape; update _parse_doc_edge_table in this file to "
            f"match."
        )
    return rows


DOC_EDGE_TABLE = _parse_doc_edge_table(_DOC_TEXT)

# The four columns the doc's table carries and this binding covers.
# addable_after_creation is a real EDGE_CLASSIFICATIONS field but has no
# column in the doc's table (the doc's prose covers it narratively per-edge
# instead — e.g. "minted once at node-add time only" in the pairing-invariant
# section) — deliberately not asserted here for that reason, not an oversight.
_BOUND_EDGE_COLUMNS = ("cascade", "provenance", "temporal_causal", "removable")


def test_edge_table_relations_match_edge_classifications_keys():
    """Every relation in EDGE_CLASSIFICATIONS appears in the doc's table,
    and vice versa — the doc's edge-partition table is a complete
    enumeration, not a partial one that happens to cover today's relations.

    If this fails because a new relation was added to EDGE_CLASSIFICATIONS,
    the doc's table needs a new row (and this test isn't wrong to demand
    one — an edge-partition doc that silently omits a real relation is
    exactly the "site nobody tests" gap #2299 exists to close). If it fails
    because the doc's table has a row for a relation no longer in
    EDGE_CLASSIFICATIONS, the doc's row is stale and should be removed (or
    moved to a History block, per this doc's own discipline for retired
    claims).
    """
    doc_relations = frozenset(DOC_EDGE_TABLE)
    code_relations = frozenset(engram_core.EDGE_CLASSIFICATIONS)
    assert doc_relations == code_relations, (
        "docs/internal/specs/2252-cb-ncb-partition.md's edge-partition table "
        f"relations ({sorted(doc_relations)}) no longer match "
        f"engram_core.EDGE_CLASSIFICATIONS's keys ({sorted(code_relations)}) "
        f"— missing from doc: {sorted(code_relations - doc_relations)}; "
        f"extra in doc: {sorted(doc_relations - code_relations)}"
    )


def test_edge_table_flags_match_edge_classifications():
    """Pins every (relation, column) cell in the doc's edge-partition table
    to the matching EDGE_CLASSIFICATIONS flag, for all four bound columns
    (cascade, provenance, temporal_causal, removable).

    This is the specific regression guard for the #2299 incident: Stage 1.3
    (#2289) flipped `supported_by`'s `cascade` flag to False in code while
    the doc's table still asserted **True**, with a caveat paragraph adding
    two confidence markers ("confirmed directly from EDGE_CLASSIFICATIONS",
    "full stop") built specifically to discourage re-checking. Had this test
    existed then, it would have gone red the moment #2289 merged instead of
    staying silently wrong until Borges caught it by hand while reviewing
    W6. Runs as one assertion per (relation, column) rather than one big
    dict-equality so a failure names exactly which cell is wrong, not just
    that something in the table is.
    """
    mismatches = []
    for relation, doc_flags in sorted(DOC_EDGE_TABLE.items()):
        code_flags = engram_core.EDGE_CLASSIFICATIONS.get(relation)
        if code_flags is None:
            # Covered by test_edge_table_relations_match_edge_classifications_keys;
            # skip here so this test's failure list stays about value
            # mismatches, not membership mismatches.
            continue
        for column in _BOUND_EDGE_COLUMNS:
            if doc_flags[column] != code_flags[column]:
                mismatches.append(
                    f"`{relation}`.{column}: doc says {doc_flags[column]}, "
                    f"EDGE_CLASSIFICATIONS says {code_flags[column]}"
                )
    assert not mismatches, (
        "docs/internal/specs/2252-cb-ncb-partition.md's edge-partition table "
        "disagrees with engram_core.EDGE_CLASSIFICATIONS on "
        f"{len(mismatches)} cell(s):\n"
        + "\n".join(f"  - {m}" for m in mismatches)
        + "\nUpdate the doc's table row(s) to match the shipped declaration "
        "— unless this is a deliberate flag change, in which case update "
        "EDGE_CLASSIFICATIONS's comment, the doc's table, AND this test's "
        "expectations together."
    )
