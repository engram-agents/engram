"""Tests for tools/propose-generalizes-migration.py (#2753 epic, WS3 D5).

Fixture shape: same discipline as test_dag_conformance.py — a minimal
on-disk sqlite db carrying only the columns the script actually reads
(nodes: id/type/claim/is_current/superseded_by/recall_summary; edges:
source_id/target_id/relation/created_at). On-disk rather than in-memory
because the script's read-only guarantee IS a `file:...?mode=ro` URI, and
testing it against an in-memory handle would test something else.

Coverage, in the order spec §1 lists it:
  1. R1 REGRESSION, BOTH HALVES MANDATORY — `cornerstone supported_by
     lesson` -> `cornerstone generalizes lesson`, and `cornerstone
     supported_by axiom` -> `axiom generalizes cornerstone`. Same relation,
     OPPOSITE directions. A fixed-direction-per-relation implementation
     passes the first and fails the second, so the first alone proves
     nothing and the two live in one test that asserts both.
  2. Same-type pair -> AMBIGUOUS, and emits NO `accept:` line.
  3. A `supersedes` (and `retracts`) edge between two lessons -> not
     proposed. Both endpoints are deliberately is_current=1 in the fixture
     so this test isolates the RELATION filter; if the superseded endpoint
     were also non-current the test would pass through the is_current gate
     instead and prove nothing about relation exclusion.
  4. An edge onto a superseded node -> not proposed (#1195 hazard).
  5. A pair already carrying `generalizes` -> not re-proposed (idempotency).
  6. An endpoint-refused pair -> appears in the report under
     ENDPOINT-REFUSED, carries no `accept:` line.
  7. Read-only enforcement: the script's own connection refuses a write.
  8. Advisory cycle dry-run (algorithm step 7), on an AMBIGUOUS pair where
     one direction is clean and the other would close a cycle.
  9. The generality order is DERIVED from GENERALIZES_ALLOWED_PAIRS, not
     restated — lock-in test for `axiom, goal > cornerstone > lesson`.
 10. THE SEAM (R3): monkeypatching GENERALIZES_ALLOWED_PAIRS to add
     ("axiom", "lesson") moves a row from ENDPOINT-REFUSED to TYPE-ORDER
     with no other change — i.e. overturning Kepler's ruling moves a tier
     boundary rather than requiring a restructure.
 11. Fail-loud on a missing DB: an empty report and an unreadable database
     must not look alike.
 12. Structural: no `--apply` flag, no write SQL anywhere in the module.

Then, from the #2862 review's mutation run — six behaviours that were
CORRECT but UNCONSTRAINED, i.e. a mutation of each passed the whole suite
above. Each carries the mutation it kills in its own docstring:
 13. `is_current` on the SOURCE endpoint (the fixture's only non-current
     node was previously never used as a source).
 14. Pair dedup: two nominating relations on one unordered pair -> ONE
     candidate, one `accept:` line.
 15. The self-edge guard (whose removal crashes rather than mis-reports).
 16. Cycle-check argument order on TYPE-ORDER — the tier that carries
     runnable `accept:` lines.
 17. The rank still assigns a direction on an ENDPOINT-REFUSED row.
 18. Already-linked suppression is direction-agnostic (the source-side
     half of idempotency).

Mutation-verification discipline (spec §5): every control here was
confirmed by deliberately breaking the implementation and observing the
test go RED, then restoring. The mutations run are recorded in the PR
handoff; a test that passes against both the correct and the broken
implementation proves nothing.

Import-pattern contract (#948 wave C): loads the hyphenated tool module by
path via importlib (it is a script, not an importable package name), and
reaches engram_core only through that module's own `core` reference so a
monkeypatched whitelist is seen by exactly the code under test.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sqlite3
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ENGRAM = os.path.join(REPO_ROOT, "src", "engram")
for _p in (SRC_ENGRAM, REPO_ROOT):
    if _p not in sys.path:
        sys.path.append(_p)

SCRIPT_PATH = os.path.join(REPO_ROOT, "tools", "propose-generalizes-migration.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "propose_generalizes_migration", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


# --- fixture ----------------------------------------------------------------

SCHEMA = """
CREATE TABLE nodes (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    claim         TEXT,
    is_current    INTEGER DEFAULT 1,
    superseded_by TEXT,
    recall_summary TEXT
);
CREATE TABLE edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    relation   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, target_id, relation)
);
"""

# (id, type, is_current, superseded_by)
NODES = [
    # 1. R1 regression — the SAME relation, the OPPOSITE directions.
    ("cs_t01", "cornerstone", 1, None),
    ("ls_t01", "lesson", 1, None),
    ("ax_t01", "axiom", 1, None),
    # 2. same-type -> AMBIGUOUS
    ("ls_t02", "lesson", 1, None),
    ("ls_t03", "lesson", 1, None),
    # 3. excluded relations. BOTH current on purpose — see module docstring.
    ("ls_t06", "lesson", 1, None),
    ("ls_t07", "lesson", 1, None),
    ("ls_t15", "lesson", 1, None),
    ("ls_t16", "lesson", 1, None),
    # 4. superseded endpoint
    ("cs_t02", "cornerstone", 1, None),
    ("ls_t04", "lesson", 0, "ls_t05"),
    ("ls_t05", "lesson", 1, None),
    # 5. idempotency
    ("cs_t03", "cornerstone", 1, None),
    ("ls_t08", "lesson", 1, None),
    # 6. endpoint-refused, both shapes
    ("ls_t09", "lesson", 1, None),
    ("ax_t02", "axiom", 1, None),
    ("ax_t03", "axiom", 1, None),
    ("gl_t01", "goal", 1, None),
    # 8. advisory cycle dry-run
    ("ls_t20", "lesson", 1, None),
    ("ls_t21", "lesson", 1, None),
    ("ls_t22", "lesson", 1, None),
    # 13. is_current on the SOURCE endpoint. ls_t04 above is non-current but
    #     is only ever a TARGET, so the source half of the gate is otherwise
    #     never exercised by this fixture.
    ("ls_t30", "lesson", 0, "ls_t31"),
    ("ls_t31", "lesson", 1, None),
    ("cs_t30", "cornerstone", 1, None),
    # 14. one unordered pair, two nominating relations, opposite directions.
    ("cs_t31", "cornerstone", 1, None),
    ("ls_t32", "lesson", 1, None),
    # 15. the self-edge guard.
    ("ls_t33", "lesson", 1, None),
    # 16. cycle-check argument order on the TYPE-ORDER tier.
    ("cs_t32", "cornerstone", 1, None),
    ("cs_t33", "cornerstone", 1, None),
    ("ls_t34", "lesson", 1, None),
]

# (source, relation, target)
EDGES = [
    # 1. R1 — cornerstone supported_by lesson  -> cs generalizes ls (SAME dir)
    ("cs_t01", "supported_by", "ls_t01"),
    #    R1 — cornerstone supported_by axiom   -> ax generalizes cs (REVERSED)
    ("cs_t01", "supported_by", "ax_t01"),
    # 2. same-type pair
    ("ls_t02", "cites", "ls_t03"),
    # 3. excluded by relation name
    ("ls_t06", "supersedes", "ls_t07"),
    ("ls_t15", "retracts", "ls_t16"),
    # 4. edge onto a superseded node
    ("cs_t02", "supported_by", "ls_t04"),
    # 5. pair already carrying generalizes (plus a live nominating relation)
    ("cs_t03", "generalizes", "ls_t08"),
    ("cs_t03", "supported_by", "ls_t08"),
    # 6a. refused, direction implied by rank: lesson cites axiom
    ("ls_t09", "cites", "ax_t02"),
    # 6b. refused, equal rank so NO direction implied: axiom cites goal
    ("ax_t03", "cites", "gl_t01"),
    # 8. existing generalizes chain ls_t20 -> ls_t21 -> ls_t22, then a
    #    nomination on the {ls_t22, ls_t20} pair. AMBIGUOUS, and one of the
    #    two directions would close ls_t20 -> ls_t21 -> ls_t22 -> ls_t20.
    ("ls_t20", "generalizes", "ls_t21"),
    ("ls_t21", "generalizes", "ls_t22"),
    ("ls_t22", "cites", "ls_t20"),
    # 13. a NON-CURRENT node used as an edge SOURCE. Type-wise this is an
    #     ordinary TYPE-ORDER nomination (cornerstone > lesson); only the
    #     endpoint state disqualifies it, so its absence is attributable.
    ("ls_t30", "cites", "cs_t30"),
    # 14. the SAME unordered pair nominated from BOTH ends. Opposite
    #     directions are load-bearing: keyed on an ordered (src, tgt) tuple,
    #     two same-direction edges would still collapse into one candidate,
    #     so a same-direction fixture cannot tell the two apart.
    ("cs_t31", "supported_by", "ls_t32"),
    ("ls_t32", "cites", "cs_t31"),
    # 15. a self-edge.
    ("ls_t33", "cites", "ls_t33"),
    # 16. an existing chain ls_t34 -> cs_t33 -> cs_t32, so the TYPE-ORDER
    #     proposal `cs_t32 generalizes ls_t34` would close a cycle while the
    #     reverse direction is clean. The asymmetry is what makes a swapped
    #     argument order visible.
    ("ls_t34", "generalizes", "cs_t33"),
    ("cs_t33", "generalizes", "cs_t32"),
    ("cs_t32", "supported_by", "ls_t34"),
]


@pytest.fixture()
def fixture_db(tmp_path):
    path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    for nid, ntype, current, sby in NODES:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, is_current, superseded_by, "
            "recall_summary) VALUES (?, ?, ?, ?, ?, ?)",
            (nid, ntype, f"claim for {nid}", current, sby, f"summary for {nid}"),
        )
    for src, rel, tgt in EDGES:
        # NB column order is (source_id, target_id, relation) but the EDGES
        # tuples read (source, relation, target) — bind explicitly rather than
        # positionally. Getting this wrong writes the relation into target_id
        # and every JOIN silently returns zero rows.
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, ?, '2026-09-12T00:00:00Z')",
            (src, tgt, rel),
        )
    conn.commit()
    conn.close()

    # Fixture self-check: the fixture is itself code, and a silently
    # mis-populated fixture makes every assertion below vacuous rather than
    # failing loudly. Assert the graph the tests think they are testing.
    check = sqlite3.connect(str(path))
    assert check.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == len(NODES)
    assert check.execute(
        "SELECT COUNT(*) FROM edges e JOIN nodes s ON s.id = e.source_id "
        "JOIN nodes t ON t.id = e.target_id"
    ).fetchone()[0] == len(EDGES), "fixture edges do not join to fixture nodes"
    assert check.execute(
        "SELECT COUNT(*) FROM edges WHERE source_id='cs_t01' "
        "AND target_id='ax_t01' AND relation='supported_by'"
    ).fetchone()[0] == 1, "fixture edge columns are transposed"
    check.close()
    return path


def run_report(db_path, tmp_path, extra=None):
    """Run the CLI end-to-end and return (exit_code, report_text)."""
    out = tmp_path / "report.md"
    argv = ["--db", str(db_path), "--out", str(out)] + list(extra or [])
    rc = mod.main(argv)
    text = out.read_text(encoding="utf-8") if out.exists() else ""
    return rc, text


# --- report parsing helpers -------------------------------------------------

TIERS = (mod.TIER_TYPE_ORDER, mod.TIER_AMBIGUOUS, mod.TIER_REFUSED)


def parse_sections(report):
    """-> [{'tier', 'header', 'body'}] for every `### C-nnn` candidate."""
    tier, cur, sections = None, None, []
    for line in report.splitlines():
        if line.startswith("## ") and " — " in line:
            name = line[3:].split(" — ")[0].strip()
            if name in TIERS:
                tier = name
            cur = None
            continue
        if line.startswith("### "):
            cur = {"tier": tier, "header": line, "body": []}
            sections.append(cur)
            continue
        if cur is not None:
            cur["body"].append(line)
    return sections


def section_for(report, *node_ids):
    """The single candidate section mentioning every given node id."""
    hits = [
        s for s in parse_sections(report)
        if all(nid in s["header"] for nid in node_ids)
    ]
    assert len(hits) == 1, (
        f"expected exactly 1 section for {node_ids}, got {len(hits)}: "
        + repr([h["header"] for h in hits])
    )
    return hits[0]


def sections_mentioning(report, node_id):
    return [
        s for s in parse_sections(report)
        if node_id in s["header"] or any(node_id in ln for ln in s["body"])
    ]


def accept_lines(section):
    return [ln for ln in section["body"] if ln.startswith("accept:")]


# --- 1. R1 regression — BOTH halves, one test -------------------------------

def test_r1_direction_comes_from_type_pair_not_relation(fixture_db, tmp_path):
    """THE load-bearing test. Both halves must be present.

    `cs_t01 supported_by ls_t01` and `cs_t01 supported_by ax_t01` are the
    SAME relation from the SAME node. A fixed-direction-per-relation
    implementation emits `cs_t01 generalizes ...` for both and is silently
    backwards on the second. Asserting only the first half would pass under
    that broken implementation, so it would prove nothing.
    """
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0

    # Half A — same direction as the source edge.
    a = section_for(report, "cs_t01", "ls_t01")
    assert a["tier"] == mod.TIER_TYPE_ORDER, a["header"]
    assert "cs_t01 generalizes ls_t01" in a["header"], a["header"]
    assert accept_lines(a) == [
        'accept: engram_add_edge("cs_t01", "generalizes", "ls_t01")'
    ], a["body"]

    # Half B — REVERSED relative to the source edge. This is the half a
    # relation-keyed implementation gets wrong.
    b = section_for(report, "cs_t01", "ax_t01")
    assert b["tier"] == mod.TIER_TYPE_ORDER, b["header"]
    assert "ax_t01 generalizes cs_t01" in b["header"], b["header"]
    assert accept_lines(b) == [
        'accept: engram_add_edge("ax_t01", "generalizes", "cs_t01")'
    ], b["body"]

    # And the source pattern is reported as it actually stands in the graph —
    # the nomination, not the proposal.
    assert "source pattern : cs_t01 --supported_by--> ax_t01" in b["body"]


# --- 2. same-type -> AMBIGUOUS, no accept: line -----------------------------

def test_same_type_pair_is_ambiguous_with_no_accept_line(fixture_db, tmp_path):
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    s = section_for(report, "ls_t02", "ls_t03")
    assert s["tier"] == mod.TIER_AMBIGUOUS, s["header"]
    assert accept_lines(s) == [], s["body"]
    assert "generalizes" not in s["header"], s["header"]
    # The source edge direction is shown, and explicitly labelled a hint.
    hint = [ln for ln in s["body"] if ln.startswith("HINT (not a rule)")]
    assert len(hint) == 1, s["body"]
    assert "ls_t02 --cites--> ls_t03" in hint[0]


def test_no_include_ambiguous_drops_the_tier(fixture_db, tmp_path):
    rc, report = run_report(fixture_db, tmp_path, ["--no-include-ambiguous"])
    assert rc == 0
    assert [s for s in parse_sections(report) if s["tier"] == mod.TIER_AMBIGUOUS] == []
    # but the TYPE-ORDER rows are untouched
    assert section_for(report, "cs_t01", "ls_t01")["tier"] == mod.TIER_TYPE_ORDER


# --- 3. supersedes / retracts are never a nomination ------------------------

def test_supersedes_and_retracts_are_not_proposed(fixture_db, tmp_path):
    """Both endpoints are is_current=1 in the fixture, so a pass here is
    attributable to the relation filter and nothing else."""
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    for nid in ("ls_t06", "ls_t07", "ls_t15", "ls_t16"):
        assert sections_mentioning(report, nid) == [], (
            f"{nid} was proposed; supersedes/retracts must never nominate"
        )
    assert "- excluded by relation (supersedes/retracts) : 2" in report


# --- 4. superseded endpoint -------------------------------------------------

def test_edge_onto_superseded_node_is_not_proposed(fixture_db, tmp_path):
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert sections_mentioning(report, "ls_t04") == []
    assert sections_mentioning(report, "cs_t02") == []
    # 2, not 1: this test's own target-side drop (cs_t02 -> ls_t04) plus the
    # source-side drop (ls_t30 -> cs_t30) added for
    # test_non_current_source_endpoint_is_dropped. The denominator is global;
    # the per-node assertions above are what keep this test attributable.
    assert "- dropped, superseded endpoint : 2" in report


# --- 5. idempotency ---------------------------------------------------------

def test_pair_already_carrying_generalizes_is_not_reproposed(fixture_db, tmp_path):
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert sections_mentioning(report, "ls_t08") == []
    assert "- dropped, pair already carries `generalizes` : 1" in report


def test_idempotency_second_run_after_acceptance(fixture_db, tmp_path):
    """Accept one proposal for real, re-run, and confirm it is gone while the
    rest survive — the "second run after partial adjudication" requirement."""
    rc, first = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert section_for(first, "cs_t01", "ls_t01")["tier"] == mod.TIER_TYPE_ORDER

    conn = sqlite3.connect(str(fixture_db))
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) "
        "VALUES ('cs_t01', 'ls_t01', 'generalizes', '2026-09-12T01:00:00Z')"
    )
    conn.commit()
    conn.close()

    rc, second = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert [
        s for s in parse_sections(second)
        if "cs_t01" in s["header"] and "ls_t01" in s["header"]
    ] == []
    # the unrelated R1 half-B row is still proposed
    assert section_for(second, "cs_t01", "ax_t01")["tier"] == mod.TIER_TYPE_ORDER


# --- 6. ENDPOINT-REFUSED is reported, never silently filtered ---------------

def test_endpoint_refused_is_reported_with_no_accept_line(fixture_db, tmp_path):
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0

    # (a) rank implies a direction, but (axiom, lesson) is not whitelisted.
    a = section_for(report, "ls_t09", "ax_t02")
    assert a["tier"] == mod.TIER_REFUSED, a["header"]
    assert accept_lines(a) == [], a["body"]
    assert any("refused because" in ln for ln in a["body"]), a["body"]
    assert any("('axiom', 'lesson')" in ln for ln in a["body"]), a["body"]

    # (b) equal rank, so no direction is implied at all.
    b = section_for(report, "ax_t03", "gl_t01")
    assert b["tier"] == mod.TIER_REFUSED, b["header"]
    assert accept_lines(b) == [], b["body"]
    assert "<->" in b["header"], b["header"]

    # Reported WITH counts — the tier header carries the number.
    assert f"## {mod.TIER_REFUSED} — 2 candidate(s)" in report


# --- 7. read-only is enforced by the connection -----------------------------

def test_connection_is_read_only(fixture_db):
    conn = mod.open_readonly(fixture_db)
    try:
        with pytest.raises(sqlite3.OperationalError) as exc:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) "
                "VALUES ('cs_t01', 'ls_t01', 'generalizes', 'now')"
            )
        assert "readonly" in str(exc.value).lower(), str(exc.value)
        # and reads still work, so the refusal above is about write mode and
        # not about a broken handle
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == len(NODES)
    finally:
        conn.close()


def test_no_write_path_exists_in_the_module():
    """Structural guard on R2: no --apply flag, no write SQL, anywhere.

    The module DOCSTRING is stripped before the SQL scan — it talks at length
    about the `--apply` flag that deliberately does not exist, and a check
    that trips on its own rationale would have to be deleted rather than
    fixed. The flag itself is checked against the real parser instead, which
    is the thing that would actually accept it.
    """
    source = open(SCRIPT_PATH, encoding="utf-8").read()
    tree = ast.parse(source)
    doc = ast.get_docstring(tree, clean=False)
    body = source.replace(doc, "", 1) if doc else source
    lowered = body.lower()
    for forbidden in ("insert into", "update ", "delete from", "drop table",
                      "mode=rw", "mode=rwc"):
        assert forbidden not in lowered, f"write path appeared: {forbidden!r}"
    assert "mode=ro" in lowered, "the read-only URI mode is gone"

    parser = mod.build_parser()
    flags = {opt for action in parser._actions for opt in action.option_strings}
    assert "--apply" not in flags, flags
    assert "--include-ambiguous" in flags and "--no-include-ambiguous" in flags, flags
    # positive control: the scan can see the body it claims to have scanned
    assert "def main(" in body


# --- 8. advisory cycle dry-run (algorithm step 7) ---------------------------

def test_cycle_dry_run_reports_per_direction(fixture_db, tmp_path):
    """ls_t20 -> ls_t21 -> ls_t22 already exist as `generalizes` edges.
    The nominated {ls_t22, ls_t20} pair is AMBIGUOUS, and exactly one of
    its two directions would close the loop."""
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    s = section_for(report, "ls_t20", "ls_t22")
    assert s["tier"] == mod.TIER_AMBIGUOUS, s["header"]
    cyc = [ln for ln in s["body"] if ln.startswith("cycle check")]
    assert len(cyc) == 2, s["body"]
    clean = [ln for ln in cyc if ln.endswith("clean")]
    dirty = [ln for ln in cyc if "WOULD CLOSE A CYCLE" in ln]
    assert len(clean) == 1 and len(dirty) == 1, cyc
    assert "ls_t20 -> ls_t22: clean" in clean[0], clean
    assert "ls_t22 -> ls_t20" in dirty[0], dirty
    # the advisory caveat is stated in the report, not just in the code
    assert "advisory" in report


# --- 9. the generality order is derived, not restated -----------------------

def test_generality_order_is_derived_from_the_whitelist():
    rank = mod.derive_generality_rank(mod.core.GENERALIZES_ALLOWED_PAIRS)
    assert rank == {"lesson": 0, "cornerstone": 1, "axiom": 2, "goal": 2}, rank
    # spec R1's stated order, asserted as relations rather than literals
    assert rank["axiom"] > rank["cornerstone"] > rank["lesson"]
    assert rank["goal"] > rank["cornerstone"]
    assert rank["axiom"] == rank["goal"]


def test_generality_order_follows_a_changed_whitelist(monkeypatch):
    """Derivation control: a whitelist with an extra rung produces a
    different order. Proves the order is computed from the input rather
    than hardcoded and merely agreeing with it by coincidence."""
    patched = frozenset({
        ("lesson", "lesson"),
        ("cornerstone", "lesson"),
        ("axiom", "cornerstone"),
        ("goal", "axiom"),          # goal now sits ABOVE axiom
    })
    rank = mod.derive_generality_rank(patched)
    assert rank == {"lesson": 0, "cornerstone": 1, "axiom": 2, "goal": 3}, rank


def test_cyclic_whitelist_fails_loud():
    with pytest.raises(ValueError, match="cycle"):
        mod.derive_generality_rank(frozenset({
            ("axiom", "cornerstone"), ("cornerstone", "axiom"),
        }))


# --- 10. THE SEAM: an overturn moves a tier boundary ------------------------

def test_overturning_the_endpoint_ruling_moves_the_tier_boundary(
    fixture_db, tmp_path, monkeypatch
):
    """R3 / Kepler's overturnable point. Lei may rule that `axiom->lesson`
    is allowed after all. If so, the ONLY edit is the whitelist — the
    ls_t09/ax_t02 row must move from ENDPOINT-REFUSED to TYPE-ORDER, gain
    a runnable accept: line, and get its direction from the same type order,
    with no change to this script."""
    baseline_rc, baseline = run_report(fixture_db, tmp_path)
    assert baseline_rc == 0
    assert section_for(baseline, "ls_t09", "ax_t02")["tier"] == mod.TIER_REFUSED

    monkeypatch.setattr(
        mod.core,
        "GENERALIZES_ALLOWED_PAIRS",
        mod.core.GENERALIZES_ALLOWED_PAIRS | {("axiom", "lesson")},
    )
    rc, overturned = run_report(fixture_db, tmp_path)
    assert rc == 0
    s = section_for(overturned, "ls_t09", "ax_t02")
    assert s["tier"] == mod.TIER_TYPE_ORDER, s["header"]
    assert "ax_t02 generalizes ls_t09" in s["header"], s["header"]
    assert accept_lines(s) == [
        'accept: engram_add_edge("ax_t02", "generalizes", "ls_t09")'
    ], s["body"]
    # the equal-rank refusal is NOT collaterally moved by that one addition
    assert section_for(overturned, "ax_t03", "gl_t01")["tier"] == mod.TIER_REFUSED


def test_whitelist_is_read_live_not_copied_at_import(fixture_db, tmp_path, monkeypatch):
    """Control for the seam: narrowing the whitelist must be visible too.
    If the script had snapshotted the frozenset at import, this would still
    report cs_t01/ls_t01 as TYPE-ORDER."""
    monkeypatch.setattr(
        mod.core,
        "GENERALIZES_ALLOWED_PAIRS",
        frozenset(
            p for p in mod.core.GENERALIZES_ALLOWED_PAIRS
            if p != ("cornerstone", "lesson")
        ),
    )
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert section_for(report, "cs_t01", "ls_t01")["tier"] == mod.TIER_REFUSED


# --- 11. fail loud on a bad path --------------------------------------------

def test_missing_db_fails_loud_not_empty_report(tmp_path, capsys):
    out = tmp_path / "report.md"
    rc = mod.main(["--db", str(tmp_path / "nope.db"), "--out", str(out)])
    assert rc == 2
    assert not out.exists(), "a bad path must not produce a report file at all"
    err = capsys.readouterr().err
    assert "not found" in err
    assert "must not look alike" in err


# --- summary line + denominators --------------------------------------------

def test_summary_line_carries_per_tier_counts(fixture_db, tmp_path, capsys):
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    printed = capsys.readouterr().out
    for tier in TIERS:
        assert f"{tier}: " in printed, printed
    assert "scanned" in printed and "already-generalizes" in printed, printed

    # The report's own denominators. A tier count with no denominator cannot
    # distinguish a healthy graph from a broken scanner.
    assert f"- principle->principle edges scanned : {len(EDGES)}" in report
    assert "- distinct pairs nominated : " in report


# ===========================================================================
# Mutation-gap closures (#2862 review). Each test below exists because a
# specific mutation of the tool passed the whole suite: the behaviour was
# correct but unconstrained. The mutation each one kills is named in its
# docstring so a future editor can re-run it rather than trust this comment.
# ===========================================================================


# --- 13. is_current is filtered on the SOURCE endpoint, not just the target -

def test_non_current_source_endpoint_is_dropped(fixture_db, tmp_path):
    """MUTATION: drop the `s_current` half of the is_current gate.

    Before this test the fixture's only non-current node (`ls_t04`) appeared
    exclusively as a TARGET, so the source half of
    `row["s_current"] == 1 and row["t_current"] == 1` could be deleted with
    no test and no report changing. `ls_t30 --cites--> cs_t30` is an
    otherwise ordinary TYPE-ORDER nomination whose ONLY disqualification is
    that its source is superseded.
    """
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert sections_mentioning(report, "ls_t30") == [], (
        "a non-current SOURCE was proposed; the #1195 hazard is symmetric "
        "and the gate must run on both endpoints"
    )
    assert sections_mentioning(report, "cs_t30") == []
    assert "- dropped, superseded endpoint : 2" in report

    # Positive control: the pair is a nomination in every respect EXCEPT the
    # endpoint state. Without this, the absence above would also be produced
    # by a pair that is simply unclassifiable, and the test would pass for
    # the wrong reason.
    rank = mod.derive_generality_rank(mod.core.GENERALIZES_ALLOWED_PAIRS)
    assert mod.classify_pair(
        "cs_t30", "ls_t30",
        {"cs_t30": "cornerstone", "ls_t30": "lesson"},
        rank, mod.core.GENERALIZES_ALLOWED_PAIRS,
    ) == (mod.TIER_TYPE_ORDER, "cs_t30", "ls_t30")


# --- 14. two nominations on one unordered pair collapse to ONE candidate ----

def test_two_relations_on_one_pair_collapse_to_one_candidate(fixture_db, tmp_path):
    """MUTATION: key the nomination dict on an ordered `(src, tgt)` tuple
    instead of `frozenset((src, tgt))`.

    Exercised by no previous test AND by no row of the author's real graph
    (34 edges -> 34 candidates), so the collapse never fired in practice
    either. `cs_t31 --supported_by--> ls_t32` and `ls_t32 --cites--> cs_t31`
    nominate the same pair from opposite ends and must yield ONE candidate
    carrying BOTH source-pattern lines. Two candidates would mean two
    IDENTICAL `accept:` lines, and an adjudicator running both hits the
    UNIQUE(source_id, target_id, relation) constraint on the second — a
    failure that looks like a tool bug at the worst possible moment.
    """
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0

    hits = [
        s for s in parse_sections(report)
        if "cs_t31" in s["header"] and "ls_t32" in s["header"]
    ]
    assert len(hits) == 1, (
        "expected ONE candidate for the pair, got "
        + repr([h["header"] for h in hits])
    )
    s = hits[0]
    assert s["tier"] == mod.TIER_TYPE_ORDER, s["header"]

    # BOTH nominating relations are reported, as they stand in the graph.
    patterns = [ln for ln in s["body"] if ln.startswith("source pattern :")]
    assert patterns == [
        "source pattern : cs_t31 --supported_by--> ls_t32",
        "source pattern : ls_t32 --cites--> cs_t31",
    ], patterns

    # ONE accept: line, in the type-derived direction, once in the whole
    # report — the duplicate is the UNIQUE-constraint hazard.
    accept = 'accept: engram_add_edge("cs_t31", "generalizes", "ls_t32")'
    assert accept_lines(s) == [accept], s["body"]
    assert report.count(accept) == 1, "duplicated accept: line"


# --- 15. the self-edge guard ------------------------------------------------

def test_self_edge_is_excluded_and_counted(fixture_db, tmp_path):
    """MUTATION: delete the `if src == tgt` guard.

    Removing it does not produce a wrong report — it produces a hard crash,
    because `frozenset((x, x))` has ONE element and `a_id, b_id =
    sorted(key)` then raises ValueError. A crash still needs a test: without
    one, the guard's removal is caught by whoever next runs the tool against
    a graph that happens to contain a self-edge, not by CI.
    """
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert sections_mentioning(report, "ls_t33") == []
    assert "- excluded self-edges : 1" in report


# --- 16. cycle-check argument order on the TYPE-ORDER tier ------------------

def test_type_order_cycle_check_uses_the_proposed_direction(fixture_db, tmp_path):
    """MUTATION: swap the arguments to `core._generalizes_cycle_path`, i.e.
    `(conn, specific, general)` instead of `(conn, general, specific)`.

    The existing cycle test covers AMBIGUOUS, which dry-runs BOTH directions
    and therefore cannot see an argument swap at all. TYPE-ORDER is the tier
    that carries runnable `accept:` lines, so a swap there silently reports
    `clean` on exactly the row an adjudicator is about to copy.

    `ls_t34 generalizes cs_t33 generalizes cs_t32` already exists, so the
    proposed `cs_t32 generalizes ls_t34` closes a loop while the reverse
    direction is clean.
    """
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0
    s = section_for(report, "cs_t32", "ls_t34")
    assert s["tier"] == mod.TIER_TYPE_ORDER, s["header"]
    assert "cs_t32 generalizes ls_t34" in s["header"], s["header"]

    cyc = [ln for ln in s["body"] if ln.startswith("cycle check")]
    assert len(cyc) == 1, s["body"]
    assert "WOULD CLOSE A CYCLE" in cyc[0], cyc
    for nid in ("cs_t32", "cs_t33", "ls_t34"):
        assert nid in cyc[0], cyc

    # The row still carries its accept: line, in the same direction the
    # cycle check was run on.
    assert accept_lines(s) == [
        'accept: engram_add_edge("cs_t32", "generalizes", "ls_t34")'
    ], s["body"]

    # Control on the asymmetry: the assertion above is about ARGUMENT ORDER,
    # not about a graph in which every direction happens to cycle.
    conn = mod.open_readonly(fixture_db)
    try:
        assert mod.core._generalizes_cycle_path(conn, "cs_t32", "ls_t34") is not None
        assert mod.core._generalizes_cycle_path(conn, "ls_t34", "cs_t32") is None
    finally:
        conn.close()


# --- 17. the rank still assigns a direction on an ENDPOINT-REFUSED row ------

def test_endpoint_refused_row_keeps_its_rank_derived_direction(fixture_db, tmp_path):
    """MUTATION: `return TIER_REFUSED, general, specific` ->
    `return TIER_REFUSED, None, None` in the differing-rank arm.

    A refusal is not a shrug: when the ranks DO differ, the row still names
    which endpoint is the general one, and the reason cites the ONE ordered
    type pair that is missing from the whitelist. Test 6 above asserts that
    `('axiom', 'lesson')` appears in the reason, but that substring SURVIVES
    the direction being dropped — the no-direction branch renders "neither
    ('lesson', 'axiom') nor ('axiom', 'lesson')", which contains it. The
    header and the single-pair form of the reason are what actually pin it.
    """
    rc, report = run_report(fixture_db, tmp_path)
    assert rc == 0

    s = section_for(report, "ls_t09", "ax_t02")
    assert s["tier"] == mod.TIER_REFUSED, s["header"]
    assert "ax_t02 generalizes ls_t09" in s["header"], s["header"]
    assert "<->" not in s["header"], s["header"]

    reason = [ln for ln in s["body"] if ln.startswith("refused because")]
    assert len(reason) == 1, s["body"]
    assert reason[0].startswith(
        "refused because: ('axiom', 'lesson') is not in"
    ), reason[0]
    assert "neither" not in reason[0], reason[0]

    # Contrast: the equal-rank row genuinely has NO direction. The two
    # shapes must not render alike, or the report cannot distinguish "we
    # know which way it would go and it is still refused" from "the types
    # imply nothing at all".
    e = section_for(report, "ax_t03", "gl_t01")
    assert e["tier"] == mod.TIER_REFUSED, e["header"]
    assert "<->" in e["header"], e["header"]
    assert any("neither" in ln for ln in e["body"]), e["body"]

    # Neither shape is ever runnable.
    assert accept_lines(s) == [] and accept_lines(e) == []


# --- 18. already-linked suppression is direction-agnostic -------------------

def test_accepted_edge_suppresses_reproposal_in_either_direction(fixture_db, tmp_path):
    """MUTATION: record already-linked pairs by ordered `(src, tgt)` tuple
    (on BOTH the `already_linked` set and the nomination key).

    `test_idempotency_second_run_after_acceptance` accepts
    `cs_t01 generalizes ls_t01`, whose direction happens to MATCH its
    nominating edge `cs_t01 --supported_by--> ls_t01`, so it passes under
    that mutation. R1 half B is the discriminating case: the nomination runs
    `cs_t01 --supported_by--> ax_t01` while the accepted edge runs the OTHER
    way, `ax_t01 generalizes cs_t01`. A direction-keyed implementation
    re-proposes an edge the adjudicator has already written — and the second
    run of a one-shot migration is precisely where that would be trusted.
    """
    rc, first = run_report(fixture_db, tmp_path)
    assert rc == 0
    s = section_for(first, "cs_t01", "ax_t01")
    assert "ax_t01 generalizes cs_t01" in s["header"], s["header"]

    # Accept it verbatim. Note the direction is the REVERSE of the
    # nominating edge that produced the row.
    conn = sqlite3.connect(str(fixture_db))
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) "
        "VALUES ('ax_t01', 'cs_t01', 'generalizes', '2026-09-12T02:00:00Z')"
    )
    conn.commit()
    conn.close()

    rc, second = run_report(fixture_db, tmp_path)
    assert rc == 0
    assert [
        sec for sec in parse_sections(second)
        if "cs_t01" in sec["header"] and "ax_t01" in sec["header"]
    ] == [], "an accepted edge was re-proposed because its direction was reversed"
    assert "- dropped, pair already carries `generalizes` : 2" in second

    # The same-direction rows are untouched, so the drop above is about
    # direction-agnostic matching and not about a blanket suppression.
    assert section_for(second, "cs_t01", "ls_t01")["tier"] == mod.TIER_TYPE_ORDER
