"""Tests for tools/migrate_person_nodes.py (#1665).

Coverage (contract items C1-C5 from the issue-1665 spec):
  C1. is_self=True forces trust_tier proposal to ('self', 'CONFIDENT', ...)
      BEFORE any keyword scan of the prose, even when the prose contains a
      keyword that today maps to a different tier (e.g. "user").
  C2. is_self=True but a conflicting, non-empty, non-'unknown' column tier
      is a REVIEW conflict naming both values -- never silently the column
      value, never silently 'self' CONFIDENT.
  C3. is_self=False/absent: tier inference (keyword path + existing-column
      path) is byte-identical to the pre-fix behavior.
  C4. Legacy `role` key in metadata is flagged under a clearly labelled
      "LEGACY KEYS TO REMOVE" line + a DECISIONS item telling the human to
      remove it after applying the update. Absent when no legacy key.
  C5. The report is dry-run only -- it never writes to the DB.

Coverage (round 2, #1665 -- folding reviewer round 1 on PR #3259, comment
5919720918):
  C6. Only a real boolean True counts as is_self. A truthy-but-not-True
      metadata.is_self value (the string "false", the string "true", the
      int 1, ...) must NOT produce the 'self' CONFIDENT proposal -- it
      proposes tier via the non-self path (column, then keywords, exactly
      as for is_self absent) with confidence REVIEW and a note naming the
      value's repr.
  C7. Mirror conflict: is_self is not True (absent, False, or a
      non-boolean truthy value) but the trust_tier column already holds
      'self' -- never silently 'self' CONFIDENT from the column. Proposes
      'self' REVIEW with a note that the column says 'self' but is_self is
      not true.
  C8. Nit fold: the C2 conflict note names the column value once, not
      twice (no test change required -- the existing C2 test does not
      assert the exact string, see test_c2_is_self_conflicts_with_different_column_tier).

Mirrors the sibling test_migrate_stranded_supported_by.py /
test_migrate_prediction_pairing_edges.py pattern: load the tool module by
file path (shadowed by the top-level tools/ package on sys.path -- see
conftest.py), provision a fresh server-backed temp DB, insert rows
directly, then exercise the tool's real report entry point (run_report)
and assert on the captured stdout.

Fixture node IDs are synthetic (pn_9xxx) -- never real ENGRAM node IDs.
"""

import atexit
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_migrate_person_nodes_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "engram", "tools", "migrate_person_nodes.py",
)


def _load_migration_module():
    """Load the script by path -- it lives under src/engram/tools/, which is
    shadowed by the top-level tools/ package on sys.path (see conftest.py),
    so a plain `import tools.migrate_person_nodes` resolves to the wrong
    package entirely."""
    spec = importlib.util.spec_from_file_location(
        "migrate_person_nodes", _SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_server():
    """Provision a clean server instance backed by a temporary directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


def _seed_person(
    server,
    db_path: str,
    node_id: str,
    claim: str,
    logical_chain: str = "",
    metadata: dict | None = None,
    trust_tier: str = "",
):
    """Directly construct a legacy-schema person node row."""
    server.core._get_db().close()  # first call creates the schema
    conn = sqlite3.connect(db_path)
    now = "2026-07-06T12:00:00Z"
    conn.execute(
        "INSERT INTO nodes "
        "(id, type, is_current, created_at, claim, logical_chain, metadata, trust_tier) "
        "VALUES (?, 'person', 1, ?, ?, ?, ?, ?)",
        (
            node_id,
            now,
            claim,
            logical_chain,
            json.dumps(metadata or {}),
            trust_tier,
        ),
    )
    conn.commit()
    conn.close()


def _run_report_capture(mod, db_path: str) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.run_report(db_path)
    return buf.getvalue()


def _db_hash(db_path: str) -> str:
    with open(db_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------------------
# C1 -- is_self forces 'self' CONFIDENT, skipping the keyword scan entirely.
# ---------------------------------------------------------------------------

def test_c1_is_self_overrides_keyword_tier_column_empty():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    # Claim contains "user", which today maps to 'primary_user' via keyword
    # match -- this is the discriminator: without the fix, this test would
    # see 'primary_user' proposed instead of 'self'.
    _seed_person(
        server,
        db_path,
        "pn_9001",
        claim="Kepler -- AI agent for the user",
        metadata={"is_self": True},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "trust_tier: 'self' [CONFIDENT" in report, report
    assert "is_self" in report.lower()
    assert "'primary_user'" not in report


def test_c1_is_self_overrides_keyword_tier_column_unknown_or_self():
    for existing_tier in ("unknown", "self"):
        server = fresh_server()
        db_path = str(server.core.DB_PATH)
        _seed_person(
            server,
            db_path,
            "pn_9002",
            claim="Kepler -- AI agent for the user",
            metadata={"is_self": True},
            trust_tier=existing_tier,
        )

        mod = _load_migration_module()
        report = _run_report_capture(mod, db_path)

        assert "trust_tier: 'self' [CONFIDENT" in report, (existing_tier, report)
        assert "'primary_user'" not in report


# ---------------------------------------------------------------------------
# C2 -- is_self True but a conflicting column tier is a REVIEW conflict.
# ---------------------------------------------------------------------------

def test_c2_is_self_conflicts_with_different_column_tier():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9003",
        claim="Kepler -- AI agent",
        metadata={"is_self": True},
        trust_tier="our_side",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "trust_tier: 'self' [REVIEW" in report, report
    # Note must name BOTH values.
    assert "self" in report and "our_side" in report
    # Never silently the column value CONFIDENT.
    assert "trust_tier: 'our_side' [CONFIDENT" not in report
    # Never silently 'self' CONFIDENT (must be REVIEW, not CONFIDENT).
    assert "trust_tier: 'self' [CONFIDENT" not in report


# ---------------------------------------------------------------------------
# C3 -- is_self False/absent: unchanged behavior (keyword path + column path).
# ---------------------------------------------------------------------------

def test_c3_unchanged_keyword_path_no_is_self():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9004",
        claim="Alex -- my daughter",
        metadata={},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert (
        "trust_tier: 'user_family' [REVIEW: inferred from keyword match in prose — verify]"
        in report
    ), report


def test_c3_unchanged_column_path_no_is_self():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9005",
        claim="Someone -- acquaintance",
        metadata={},
        trust_tier="known_external",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert (
        "trust_tier: 'known_external' [CONFIDENT: taken from nodes.trust_tier column]"
        in report
    ), report


def test_c3_is_self_false_explicit_unchanged():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9006",
        claim="Alex -- my daughter",
        metadata={"is_self": False},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert (
        "trust_tier: 'user_family' [REVIEW: inferred from keyword match in prose — verify]"
        in report
    ), report


# ---------------------------------------------------------------------------
# C4 -- legacy `role` key flagged; absent when no legacy key present.
# ---------------------------------------------------------------------------

def test_c4_legacy_role_key_flagged():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9007",
        claim="Alex -- collaborator",
        metadata={"role": "old-style-role-value"},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "LEGACY KEYS TO REMOVE: role" in report, report
    # A DECISIONS item telling the human to remove it after applying the update.
    assert "remove" in report.lower() and "role" in report


def test_c4_no_legacy_key_no_section():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9008",
        claim="Alex -- collaborator",
        metadata={},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "LEGACY KEYS" not in report, report


def test_c4_legacy_key_set_is_module_level_tuple():
    mod = _load_migration_module()
    assert hasattr(mod, "_LEGACY_PERSON_KEYS")
    assert mod._LEGACY_PERSON_KEYS == ("role",)


# ---------------------------------------------------------------------------
# C5 -- dry-run only: the report never writes to the DB.
# ---------------------------------------------------------------------------

def test_c5_report_writes_nothing_to_db():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9009",
        claim="Kepler -- AI agent for the user",
        metadata={"is_self": True, "role": "legacy-role"},
        trust_tier="our_side",
    )

    before_hash = _db_hash(db_path)
    conn = sqlite3.connect(db_path)
    before_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    conn.close()

    mod = _load_migration_module()
    _run_report_capture(mod, db_path)

    after_hash = _db_hash(db_path)
    conn = sqlite3.connect(db_path)
    after_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    conn.close()

    assert before_count == after_count, (before_count, after_count)
    assert before_hash == after_hash, "run_report must not write to the DB"


# ---------------------------------------------------------------------------
# C6 -- only a real boolean True counts as is_self; a truthy-but-not-True
# value must NOT produce the 'self' CONFIDENT proposal (#1665 r2, folding
# PR #3259 reviewer-round-1 suggestion).
# ---------------------------------------------------------------------------

def test_c6_is_self_string_false_not_treated_as_self():
    """The dangerous case: today `meta.get("is_self", False)` treats the
    JSON string "false" as truthy, so it falls straight into the is_self=True
    branch and yields 'self' CONFIDENT. It must not."""
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    # Column empty -> falls through to the keyword path. Claim contains
    # "user", which matches 'primary_user' via keyword -- this is the
    # discriminator for "propose tier by the non-self path... exactly as
    # for is_self absent".
    _seed_person(
        server,
        db_path,
        "pn_9010",
        claim="Kepler -- AI agent for the user",
        metadata={"is_self": "false"},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    # Never the self CONFIDENT proposal.
    assert "trust_tier: 'self'" not in report, report
    # Proposed via the non-self (keyword) path, same tier value as if
    # is_self were absent.
    assert "trust_tier: 'primary_user' [REVIEW" in report, report
    # Note names the non-boolean value's repr.
    assert "non-boolean value: 'false'" in report, report


def test_c6_is_self_int_1_not_treated_as_self():
    """Second required C6 case: a non-boolean truthy int. Column is
    non-empty here, exercising the "column, then keywords" non-self path's
    COLUMN branch specifically (C6 covers both branches)."""
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9011",
        claim="Someone -- acquaintance",
        metadata={"is_self": 1},
        trust_tier="known_external",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "trust_tier: 'self'" not in report, report
    # Same tier value the column path would give for is_self absent, but
    # confidence downgraded to REVIEW (never silently CONFIDENT) because of
    # the non-boolean is_self value.
    assert "trust_tier: 'known_external' [REVIEW" in report, report
    assert "trust_tier: 'known_external' [CONFIDENT" not in report, report
    assert "non-boolean value: 1" in report, report


# ---------------------------------------------------------------------------
# C7 -- mirror conflict: trust_tier column == 'self' but is_self is not
# True (absent or False) -- never silently 'self' CONFIDENT from the
# column (#1665 r2, folding PR #3259 reviewer-round-1 pre-existing gap).
# ---------------------------------------------------------------------------

def test_c7_mirror_conflict_column_self_is_self_absent():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9012",
        claim="Kepler -- AI agent",
        metadata={},
        trust_tier="self",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    # Proposes the column value 'self', but REVIEW, never CONFIDENT.
    assert "trust_tier: 'self' [REVIEW" in report, report
    assert "trust_tier: 'self' [CONFIDENT" not in report, report
    # Note names the mirror conflict explicitly.
    assert "column is 'self'" in report, report
    assert "is_self is not True" in report, report


def test_c7_mirror_conflict_column_self_is_self_false():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9013",
        claim="Kepler -- AI agent",
        metadata={"is_self": False},
        trust_tier="self",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "trust_tier: 'self' [REVIEW" in report, report
    assert "trust_tier: 'self' [CONFIDENT" not in report, report
    assert "column is 'self'" in report, report
    assert "is_self is not True" in report, report


# ---------------------------------------------------------------------------
# C9 -- the report's separate is_self display line must also honour "only a
# real boolean True counts as is_self" (#1665 r3, folding a gap the r2 fairy
# flagged as out of its scope). Before this fix, the display line at
# run_report's "if is_self: print(...True [CONFIDENT])" used truthiness, so
# a legacy node with metadata.is_self: "false" printed
# "is_self:    True [CONFIDENT]" right next to a trust_tier line saying
# is_self is non-boolean -- the report contradicted itself.
# ---------------------------------------------------------------------------

def test_c9_is_self_string_false_display_line_not_true():
    """The dangerous case: today the display line's `if is_self:` treats the
    JSON string "false" as truthy and prints the True/CONFIDENT line. It
    must not."""
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9014",
        claim="Kepler -- AI agent for the user",
        metadata={"is_self": "false"},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "is_self:    True" not in report, report
    # Instead flags the non-boolean value for review.
    assert "is_self:    'false' [REVIEW" in report, report
    assert "non-boolean" in report, report


def test_c9_is_self_true_display_line_unchanged():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9015",
        claim="Kepler -- AI agent",
        metadata={"is_self": True},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "is_self:    True [CONFIDENT]" in report, report


def test_c9_is_self_absent_no_display_line():
    server = fresh_server()
    db_path = str(server.core.DB_PATH)
    _seed_person(
        server,
        db_path,
        "pn_9016",
        claim="Alex -- my daughter",
        metadata={},
        trust_tier="",
    )

    mod = _load_migration_module()
    report = _run_report_capture(mod, db_path)

    assert "is_self:" not in report, report
