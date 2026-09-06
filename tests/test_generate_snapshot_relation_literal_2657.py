"""#2657 -- fourth sibling of #2624/#2618: _generate_snapshot()'s "Derives
from:" line hardcoded `relation IN ('derives_from', 'cites')`, predating
`acknowledged_derives_from`, so a premise reached only via that relation
rendered ABSENT from a label asserting completeness.

Fix: the query now ranges over `CITATION_SUPPORT_RELATIONS | {'cites'}` --
the one-hop premise set #2618/#2624 established, unioned with `cites`
(which is not a premise relation and not in that constant) to preserve
today's rendered shape while adding the missing premise relation. Per the
issue's own three-option framing, this is "the smallest change consistent
with the label as written."

Coverage:
  1. A premise reached only via `acknowledged_derives_from` now appears in
     the "Derives from:" line (the live gap).
  2. A `cites` edge still appears too (Option A must not silently drop
     `cites` from the rendered output -- that would be Option B by
     accident, not the option this issue picked).
  3. Sourcing proof: monkeypatching `CITATION_SUPPORT_RELATIONS` to a
     decoy set (still unioned with 'cites') changes which premise renders,
     proving the query reads the constant rather than a hardcoded literal.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_generate_snapshot_2657_")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim, url):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _derive(server, claim, supporting_ids):
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids),
        "logical_chain": "Test chain.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derive failed: {r}"
    return r["derivation_id"]


def _add_edge(server, source_id, target_id, relation):
    conn = server._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (source_id, target_id, relation),
        )
        conn.commit()
    finally:
        conn.close()


def test_derives_from_line_shows_acknowledged_derives_from_premise():
    """The live gap: a derivation's second premise, reached only via
    acknowledged_derives_from, must appear in the 'Derives from:' line --
    the label asserts completeness the old literal did not honour."""
    import engram_core as core

    server = fresh_server()
    ax1 = _add_observation(server, "Premise one.", "https://example.com/ev-1")
    ax2 = _add_observation(server, "Premise two, acknowledged.", "https://example.com/ev-2")
    dv1 = _derive(server, "Derivation with an acknowledged second premise.", [ax1])
    _add_edge(server, dv1, ax2, "acknowledged_derives_from")

    conn = server._get_db()
    try:
        snapshot = core._generate_snapshot(conn)
    finally:
        conn.close()

    derives_line = next(
        (line for line in snapshot.splitlines() if line.startswith("Derives from:")),
        None,
    )
    assert derives_line is not None, f"no 'Derives from:' line found in snapshot:\n{snapshot}"
    assert ax1 in derives_line and ax2 in derives_line, (
        f"expected both {ax1} and {ax2} in the Derives-from line, got: "
        f"{derives_line!r} -- the acknowledged_derives_from premise is "
        f"invisible to the render (#2657 live gap)"
    )


def test_derives_from_line_shows_supported_by_only_premise():
    """Reviewer-fairy round-1 catch: choosing CITATION_SUPPORT_RELATIONS
    (rather than a narrower hand-picked subset matching only the issue's
    own named example) also newly renders a premise reached ONLY via
    supported_by -- a genuine one-hop premise relation
    engram_core.py:865-877 already establishes as belonging in this
    constant (the same one _direct_premise_ids ranges over), but never
    named in the issue's "Consequence" section or covered by this file's
    other two tests. Correct behavior, not a defect -- but an unstated,
    previously-untested consequence of the choice this PR made, worth its
    own live-gap test rather than riding along uncovered."""
    import engram_core as core

    server = fresh_server()
    ax = _add_observation(server, "Premise reached only via supported_by.", "https://example.com/ev-7")
    # engram_derive requires >=1 supporting_id -- cite a throwaway premise
    # to satisfy that, then remove its edge so the ONLY remaining premise
    # is the supported_by one this test actually exercises.
    throwaway = _add_observation(server, "Throwaway premise, removed below.", "https://example.com/ev-8")
    dv1 = _derive(server, "Derivation whose only premise arrives via supported_by.", [throwaway])
    conn = server._get_db()
    try:
        conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'derives_from'",
            (dv1, throwaway),
        )
        conn.commit()
    finally:
        conn.close()
    _add_edge(server, dv1, ax, "supported_by")

    conn = server._get_db()
    try:
        snapshot = core._generate_snapshot(conn)
    finally:
        conn.close()

    derives_line = next(
        (line for line in snapshot.splitlines() if line.startswith("Derives from:")),
        None,
    )
    assert derives_line is not None, (
        f"no 'Derives from:' line found in snapshot -- a supported_by-only "
        f"premise is invisible to the render, even though supported_by is "
        f"a genuine one-hop premise relation:\n{snapshot}"
    )
    assert ax in derives_line, (
        f"expected {ax} (reached only via supported_by) in the "
        f"Derives-from line, got: {derives_line!r}"
    )


def test_derives_from_line_still_shows_a_cites_edge():
    """Option A (CITATION_SUPPORT_RELATIONS | {'cites'}) must preserve
    today's rendered shape -- a genuine `cites` edge, not a premise
    relation at all, must still appear. If the fix had silently dropped
    'cites' (Option B by accident), this would regress a real, currently-
    shown value with no issue asking for that change."""
    import engram_core as core

    server = fresh_server()
    ax1 = _add_observation(server, "Premise one.", "https://example.com/ev-3")
    cited = _add_observation(server, "Something merely cited, not a premise.", "https://example.com/ev-4")
    dv1 = _derive(server, "Derivation that also cites something.", [ax1])
    _add_edge(server, dv1, cited, "cites")

    conn = server._get_db()
    try:
        snapshot = core._generate_snapshot(conn)
    finally:
        conn.close()

    derives_line = next(
        (line for line in snapshot.splitlines() if line.startswith("Derives from:")),
        None,
    )
    assert derives_line is not None, f"no 'Derives from:' line found in snapshot:\n{snapshot}"
    assert ax1 in derives_line and cited in derives_line, (
        f"expected both the real premise {ax1} and the cited node {cited} "
        f"in the Derives-from line, got: {derives_line!r} -- 'cites' was "
        f"dropped from the rendered output, a shape regression the issue "
        f"did not ask for"
    )


def test_derives_from_line_sources_from_the_module_constant(monkeypatch):
    """Sourcing proof: monkeypatching CITATION_SUPPORT_RELATIONS to a decoy
    set (excluding 'derives_from' and 'acknowledged_derives_from', still
    unioned with 'cites' by the site's own code) must remove a genuine
    derives_from-only premise from the rendered line while a 'cites' edge
    still shows -- proving the query reads the constant, not a hardcoded
    literal (which would be immune to this monkeypatch and keep showing
    the derives_from premise regardless)."""
    import engram_core as core

    server = fresh_server()
    ax1 = _add_observation(server, "Plain premise.", "https://example.com/ev-5")
    cited = _add_observation(server, "Cited node.", "https://example.com/ev-6")
    dv1 = _derive(server, "Derivation for the sourcing proof.", [ax1])
    _add_edge(server, dv1, cited, "cites")

    monkeypatch.setattr(core, "CITATION_SUPPORT_RELATIONS", frozenset({"supported_by"}))

    conn = server._get_db()
    try:
        snapshot = core._generate_snapshot(conn)
    finally:
        conn.close()

    derives_line = next(
        (line for line in snapshot.splitlines() if line.startswith("Derives from:")),
        None,
    )
    assert derives_line is not None, f"no 'Derives from:' line found in snapshot:\n{snapshot}"
    assert ax1 not in derives_line, (
        "the query did not react to the CITATION_SUPPORT_RELATIONS "
        "monkeypatch (the plain derives_from premise still renders) -- it "
        "may still hold a hardcoded literal rather than reading the "
        "module constant"
    )
    assert cited in derives_line, (
        f"expected the 'cites' edge to survive the monkeypatch (it's "
        f"unioned in separately, not part of CITATION_SUPPORT_RELATIONS), "
        f"got: {derives_line!r}"
    )
