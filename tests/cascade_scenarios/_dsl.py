"""tests/cascade_scenarios/_dsl.py — matchers for cascade behaviour cases.

Every matcher fails with a message that names the node, the property and
expected-vs-actual in plain words, so a red case reads as a finding.

Setup helpers re-export the playbox so each case file needs one import.
"""

from __future__ import annotations

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
for p in (_REPO_ROOT, _TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import fresh_module  # noqa: E402,F401  (side effect: src/engram on sys.path)

import engram_core  # noqa: E402,F401
import engram_revision  # noqa: E402,F401
import engram_observation  # noqa: E402,F401
import engram_derive  # noqa: E402,F401
import engram_conformance  # noqa: E402,F401

from cascade_playbox import cascade_playbox  # noqa: E402,F401

__all__ = [
    "cascade_playbox",
    "expect_ok", "expect_refused",
    "expect_frontier", "expect_pending", "expect_resolved", "expect_nothing_pending",
    "expect_edges", "expect_confidence", "expect_node_count", "expect_clean",
    "snapshot_confidence", "node_count",
]


# -- verb results -----------------------------------------------------------


def expect_ok(result: dict, what: str = "verb"):
    assert "error" not in result, f"{what} was refused: {result.get('error')}"
    return result


def expect_refused(result: dict, what: str = "verb", mentions: str | None = None):
    assert "error" in result, f"{what} should have been refused but succeeded: {result}"
    if mentions:
        assert mentions in result["error"], (
            f"{what} refused, but the message does not mention {mentions!r}: {result['error']}"
        )
    return result


# -- frontier / markers -----------------------------------------------------


def _frontier_names(box, f):
    return sorted(box._handle_of(e["id"]) or e["id"] for e in f["frontier"])


def expect_frontier(box, names, *, node=None, stage=""):
    f = box.frontier(box[node] if node else None)
    got = _frontier_names(box, f)
    want = sorted(names)
    assert got == want, (
        f"[{stage}] frontier{' (mode C on ' + node + ')' if node else ''}: "
        f"expected {want}, got {got}"
    )
    assert "invariant_1_violation_suspected" not in f, (
        f"[{stage}] frontier read raised invariant_1_violation_suspected: {f}"
    )
    return f


def _pending(box, name):
    return [m for m in box.markers(box[name]) if m["status"] == "pending"]


def expect_pending(box, name, *, events: int, stage=""):
    pend = _pending(box, name)
    got = len({m["cascade_event"] for m in pend})
    assert got == events, (
        f"[{stage}] {name}: expected {events} pending event(s), got {got} "
        f"(roots: {[m['root_node_handle'] for m in pend]})"
    )


def expect_resolved(box, name, *, kind: str, stage=""):
    rows = box.markers(box[name])
    assert rows, f"[{stage}] {name}: no markers at all — it was never in a cascade"
    pend = [m for m in rows if m["status"] == "pending"]
    assert not pend, f"[{stage}] {name}: still has {len(pend)} pending marker(s)"
    kinds = {m["resolution_kind"] for m in rows}
    assert kinds == {kind}, f"[{stage}] {name}: resolved as {kinds}, expected {{{kind!r}}}"


def expect_nothing_pending(box, *, stage=""):
    f = box.frontier()
    assert f["frontier_count"] == 0 and f["total_pending_nodes"] == 0, (
        f"[{stage}] expected nothing pending; frontier={_frontier_names(box, f)} "
        f"total_pending_nodes={f['total_pending_nodes']}"
    )
    assert "invariant_1_violation_suspected" not in f


# -- edges / confidence / nodes --------------------------------------------


def expect_edges(box, relation: str, pairs, *, stage=""):
    got = sorted(
        (e.get("source_handle") or e["source_id"], e.get("target_handle") or e["target_id"])
        for e in box.edges(relations=[relation])
    )
    want = sorted(tuple(p) for p in pairs)
    assert got == want, f"[{stage}] {relation} edges: expected {want}, got {got}"


def snapshot_confidence(box, names):
    return {n: box.confidence(box[n]) for n in names}


def expect_confidence(box, name, *, changed_from=None, unchanged_from=None, stage=""):
    now = box.confidence(box[name])
    if changed_from is not None:
        assert now != changed_from, (
            f"[{stage}] {name}: confidence should have moved from {changed_from}, still {now}"
        )
    if unchanged_from is not None:
        assert now == unchanged_from, (
            f"[{stage}] {name}: confidence should be unchanged at {unchanged_from}, got {now}"
        )
    return now


def node_count(box) -> int:
    conn = box._core._get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        conn.close()


def expect_node_count(box, n: int, *, stage=""):
    got = node_count(box)
    assert got == n, f"[{stage}] node table: expected {n} rows, got {got}"


def expect_clean(box, *, stage=""):
    report = box.conformance()
    dirty = {k: v for k, v in report.items() if isinstance(v, dict) and v.get("count")}
    assert not dirty, f"[{stage}] conformance red flags: {dirty}"
    cyc = box.cycles()
    assert cyc == [], f"[{stage}] cascade cycle(s) present: {cyc}"
