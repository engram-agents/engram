"""#2252 Stage 1.0 residue — tools/surgical.py's confidence-recompute relation
sets are DERIVED from engram_core, not hand-enumerated literals.

Background: #2255 collapsed the nine hardcoded cascade-walk literals inside
src/engram/ to derive from EDGE_CLASSIFICATIONS, but tools/surgical.py sat
outside that PR's file list and kept two independent hand-rolled literals:

  - _get_downstream_nodes: ('derives_from', 'cites', 'supported_by')
      Missing acknowledged_derives_from (the #2349 gap, one file over) AND
      wrongly including 'cites', which is not a premise relation at all
      (engram_core._direct_premise_ids asks the identical question in the
      forward direction and deliberately excludes it).
  - _compute_confidence_for_node's derivation branch: relation = 'derives_from'
      Missing acknowledged_derives_from, same #2349 gap, second call site.

Fix: both now derive from engram_core's own constants --
CITATION_SUPPORT_RELATIONS (derives_from, supported_by,
acknowledged_derives_from) for the downstream walk, mirroring
_direct_premise_ids; CASCADE_RELATIONS (derives_from,
acknowledged_derives_from) for the confidence-formula premise read,
mirroring engram_core's own canonical recompute function.

Coverage:
  1. _get_downstream_nodes finds a dependent reached only via
     acknowledged_derives_from (the live gap).
  2. _get_downstream_nodes does NOT return a node connected only via cites
     (the wrongly-included member).
  3. Sourcing proof (checked to be able to FAIL): monkeypatching
     CITATION_SUPPORT_RELATIONS in the surgical module to a decoy set changes
     what the downstream walk returns -- if the SQL were still a hardcoded
     literal, this monkeypatch would have no effect.
  4. _compute_confidence_for_node's derivation branch picks up a premise
     reached only via acknowledged_derives_from.
  5. Sourcing proof for CASCADE_RELATIONS, same shape as (3).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"


def _load_surgical():
    """Import tools/surgical.py fresh, isolated from any cached module.

    #2664: does NOT touch sys.path -- an earlier version did
    `sys.path.insert(0, str(SRC_ENGRAM))` here, called fresh per-test via
    the `surgical` fixture below, with no restore. That insert(0) was
    never actually needed: tests/conftest.py already puts SRC_ENGRAM on
    sys.path unconditionally for the whole session (position 1, behind
    REPO_ROOT) before any test file runs -- surgical.py's own top-level
    `from engram_confidence import ...` / `from engram_core import ...`
    resolve fine from that position, since nothing else on sys.path
    shadows those names. The insert(0)'s ONLY actual effect was
    reordering SRC_ENGRAM AHEAD of REPO_ROOT, permanently, for every test
    file collected after this one in the same session -- exactly the
    #2565 collision shape (verified live: reproduced 5 passed/53 errors
    running this file with tests/test_engine_phase3.py before this fix;
    58 passed/0 errors after). Confirmed this file's own 5 tests still
    pass with the line removed entirely, rather than swapping it for
    .append(...) or a restore-after-use pattern -- both would have
    worked, but neither is needed once the insert itself is redundant."""
    sys.modules.pop("surgical", None)
    spec = importlib.util.spec_from_file_location(
        "surgical", str(SRC_ENGRAM / "tools" / "surgical.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def surgical():
    return _load_surgical()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, confidence REAL, "
        "quote_type TEXT, metadata TEXT)"
    )
    c.execute(
        "CREATE TABLE edges (source_id TEXT, target_id TEXT, relation TEXT)"
    )
    yield c
    c.close()


def _insert_node(conn, node_id, ntype, confidence=None, metadata=None):
    conn.execute(
        "INSERT INTO nodes (id, type, confidence, metadata) VALUES (?, ?, ?, ?)",
        (node_id, ntype, confidence, json.dumps(metadata or {})),
    )


def _insert_edge(conn, source_id, target_id, relation):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation) VALUES (?, ?, ?)",
        (source_id, target_id, relation),
    )


def test_downstream_nodes_sees_acknowledged_derives_from(surgical, conn):
    """The live gap: a dependent reached only via acknowledged_derives_from
    must be found -- this is the #2349 defect class, one file over."""
    _insert_node(conn, "ob_root", "observation_factual")
    _insert_node(conn, "dv_dependent", "derivation")
    _insert_edge(conn, "dv_dependent", "ob_root", "acknowledged_derives_from")
    conn.commit()

    downstream = surgical._get_downstream_nodes(conn, "ob_root")
    assert "dv_dependent" in downstream


def test_downstream_nodes_excludes_cites(surgical, conn):
    """cites is not a premise relation -- a node connected only via cites
    must NOT be treated as a confidence-recompute dependent."""
    _insert_node(conn, "ob_root", "observation_factual")
    _insert_node(conn, "dv_citer", "derivation")
    _insert_edge(conn, "dv_citer", "ob_root", "cites")
    conn.commit()

    downstream = surgical._get_downstream_nodes(conn, "ob_root")
    assert "dv_citer" not in downstream


def test_downstream_nodes_sources_from_citation_support_relations(
    surgical, conn, monkeypatch
):
    """Sourcing proof, checked to be able to FAIL against a STRONGER decoy
    than a plain revert. The edge here is 'supported_by' -- a member of
    BOTH the correct CITATION_SUPPORT_RELATIONS default AND the OLD
    hardcoded literal this PR removes. Monkeypatching the constant to
    EXCLUDE 'supported_by' and asserting the dependent disappears is a
    real sourcing proof: a hardcoded implementation -- whether the old
    buggy literal, or a hypothetical 'imports the constant but still
    queries a literal' half-fix -- would keep finding 'supported_by' and
    fail this assertion, since neither reads the module attribute at call
    time. Using acknowledged_derives_from here instead would be a WEAKER
    proof: the old literal never included it, so a decoy could pass by
    accident rather than by genuinely deriving (the reviewer's catch,
    PR #2618)."""
    _insert_node(conn, "ob_root", "observation_factual")
    _insert_node(conn, "dv_dependent", "derivation")
    _insert_edge(conn, "dv_dependent", "ob_root", "supported_by")
    conn.commit()

    monkeypatch.setattr(
        surgical, "CITATION_SUPPORT_RELATIONS",
        frozenset({"derives_from", "acknowledged_derives_from"}),
    )
    downstream = surgical._get_downstream_nodes(conn, "ob_root")
    assert "dv_dependent" not in downstream


def test_compute_confidence_sees_acknowledged_derives_from_premise(surgical, conn):
    """The second #2349-class gap: a derivation's confidence formula must
    read a premise reached only via acknowledged_derives_from, matching
    engram_core.py's own canonical recompute (CASCADE_RELATIONS)."""
    _insert_node(conn, "ob_premise", "observation_factual", confidence=0.85)
    _insert_node(
        conn,
        "dv_node",
        "derivation",
        metadata={"reasoning_type": "deductive_modus_ponens"},
    )
    _insert_edge(conn, "dv_node", "ob_premise", "acknowledged_derives_from")
    conn.commit()

    dv_row = dict(
        conn.execute("SELECT * FROM nodes WHERE id = ?", ("dv_node",)).fetchone()
    )
    computed = surgical._compute_confidence_for_node(conn, dv_row)
    # A found premise (0.85) must move the result off the no-premises
    # fallback (0.5) -- proves the acknowledged_derives_from edge was read.
    assert computed is not None
    assert computed != 0.5


def test_compute_confidence_sources_from_cascade_relations(surgical, conn, monkeypatch):
    """Sourcing proof, checked to be able to FAIL against a STRONGER decoy
    than a plain revert. The edge here is 'derives_from' -- the one member
    the OLD hardcoded `relation = 'derives_from'` literal this PR removes
    would ALSO match. Monkeypatching CASCADE_RELATIONS to EXCLUDE
    'derives_from' and asserting the premise disappears is a real sourcing
    proof: a hardcoded implementation -- the old literal, or a hypothetical
    half-fix that imports the constant but still queries a literal -- would
    keep finding it and fail this assertion. Using
    acknowledged_derives_from here instead would be weaker: the old literal
    never looked for it, so a decoy could pass by accident (the reviewer's
    catch, PR #2618)."""
    _insert_node(conn, "ob_premise", "observation_factual", confidence=0.85)
    _insert_node(
        conn,
        "dv_node",
        "derivation",
        metadata={"reasoning_type": "deductive_modus_ponens"},
    )
    _insert_edge(conn, "dv_node", "ob_premise", "derives_from")
    conn.commit()

    # #3531: surgical no longer owns the premise query; it calls
    # engram_core._recompute_premise_ids, the single definition, which reads
    # engram_core.CASCADE_RELATIONS. Patch the constant where it is read.
    import engram_core
    monkeypatch.setattr(engram_core, "CASCADE_RELATIONS", frozenset({"acknowledged_derives_from"}))
    dv_row = dict(
        conn.execute("SELECT * FROM nodes WHERE id = ?", ("dv_node",)).fetchone()
    )
    computed = surgical._compute_confidence_for_node(conn, dv_row)
    # No premise found under the decoy set -> falls back to 0.5.
    assert computed == 0.5
