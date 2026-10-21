"""Contract tests for engram-alpha#2339: a truncated cascade walk must not
look like a complete one.

The contract (written before the implementation):

  * ``engram_core.CascadeWalkTruncated`` exists and carries ``visited`` and
    ``remaining_queue`` counts.
  * ``_walk_cascade_upstream(..., strict=True)`` raises it when the per-dequeue
    budget hits 0 while the queue is still non-empty.
  * ``strict=False`` (the default) keeps its historical return value and writes
    exactly ONE line to stderr (never stdout: MCP stdio).
  * A walk that finishes exactly as the budget hits 0 with an EMPTY queue is
    complete: no raise, no stderr line.
  * ``engram_revision._cascade_blocked`` is the single blocked-decision and
    walks strict, returning BLOCKED / CLEAR / UNKNOWN_TRUNCATED. On truncation
    (UNKNOWN_TRUNCATED, which callers withhold) it appends a warning to the caller's
    ``warnings`` list, which ``_cascade_frontier_impl`` surfaces as the
    ``cascade_walk_truncated`` response field (present only on trouble, the
    same shape as ``invariant_1_violation_suspected``).

Fixtures use the cascade playbox (``tests/cascade_playbox.py``): real nodes
through the canonical creation tools, a derivation chain of known length.

Import-pattern contract (R1): imports engram_core and engram_revision at top
level; no ``import server``.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402,F401 — sets sys.path for engram_*

import pytest  # noqa: E402

import engram_core  # noqa: E402,F401
import engram_revision  # noqa: E402,F401
from cascade_playbox import cascade_playbox  # noqa: E402


_CHAIN_DERIVATIONS = 5  # o0 <- d1 <- d2 <- d3 <- d4 <- d5 (+ whatever o0 cites)


def _build_chain(box):
    """o0 <- d1 <- ... <- d5. Returns (tip_id, root_id)."""
    box.node("o0", kind="observation")
    prev = "o0"
    for i in range(1, _CHAIN_DERIVATIONS + 1):
        box.node(f"d{i}", kind="derivation", supports=[box[prev]])
        prev = f"d{i}"
    return box.id_of(box[prev]), box.id_of(box["o0"])


def _all(row):
    return True


def _walk(box, conn, start, **kw):
    return box._core._walk_cascade_upstream(
        conn, start, collect_if=_all, descend_if=_all, **kw
    )


def _full_size(box, conn, start):
    """Number of nodes the complete walk reaches (self-inclusive)."""
    return len(_walk(box, conn, start))


# ---------------------------------------------------------------------------
# T1 — chain longer than the budget
# ---------------------------------------------------------------------------


def test_T1_strict_raises_with_counts(capsys):
    with cascade_playbox() as box:
        tip, _root = _build_chain(box)
        conn = box._core._get_db()
        try:
            n = _full_size(box, conn, tip)
            assert n > 3, "fixture must be longer than the budget under test"
            capsys.readouterr()
            with pytest.raises(box._core.CascadeWalkTruncated) as ei:
                _walk(box, conn, tip, max_depth=3, strict=True)
            exc = ei.value
            assert exc.visited == 3
            assert exc.remaining_queue == 1  # a pure chain: one frontier node
            # strict mode raises; it does not also print a stderr line
            assert capsys.readouterr().err == ""
        finally:
            conn.close()


def test_T1_nonstrict_returns_partial_and_one_stderr_line(capsys):
    with cascade_playbox() as box:
        tip, root = _build_chain(box)
        conn = box._core._get_db()
        try:
            full = _walk(box, conn, tip)
            capsys.readouterr()
            partial = _walk(box, conn, tip, max_depth=3)
            captured = capsys.readouterr()
            assert partial < full and tip in partial and root not in partial
            assert len(partial) == 3
            assert captured.out == ""  # stdout is the MCP stdio channel
            lines = [l for l in captured.err.splitlines() if l.strip()]
            assert len(lines) == 1
            assert "_walk_cascade_upstream" in lines[0]
            assert "3" in lines[0]  # the budget
            assert "1" in lines[0]  # the remaining queue size
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# T2 — boundary: budget exactly fits, queue empty at the end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [True, False])
def test_T2_exact_fit_is_complete_not_truncated(capsys, strict):
    with cascade_playbox() as box:
        tip, _root = _build_chain(box)
        conn = box._core._get_db()
        try:
            n = _full_size(box, conn, tip)
            capsys.readouterr()
            got = _walk(box, conn, tip, max_depth=n, strict=strict)
            assert len(got) == n
            assert capsys.readouterr().err == ""
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# T3 / T4 — _cascade_blocked
# ---------------------------------------------------------------------------


def test_T3_blocked_when_only_pending_ancestor_is_beyond_budget():
    with cascade_playbox() as box:
        tip, root = _build_chain(box)
        conn = box._core._get_db()
        try:
            rev = box._rev
            # control: with the default budget the ancestor is reached,
            # the node is genuinely blocked, and there is nothing to warn about
            ok_warnings: list = []
            r = rev._cascade_blocked(conn, tip, {root}, warnings=ok_warnings)
            assert r.state == rev.BLOCKED and r.blocking == (root,)
            assert ok_warnings == []

            # truncated: root lies beyond a budget of 3
            warnings: list = []
            r = rev._cascade_blocked(
                conn, tip, {root}, warnings=warnings, max_depth=3
            )
            assert r.state == rev.UNKNOWN_TRUNCATED
            assert r.truncated.budget == 3 and r.truncated.remaining_queue == 1
            assert r.blocking == ()
            assert len(warnings) == 1
            assert tip in warnings[0]
        finally:
            conn.close()


def test_T3_truncation_without_warnings_list_still_blocks():
    with cascade_playbox() as box:
        tip, root = _build_chain(box)
        conn = box._core._get_db()
        try:
            assert box._rev._cascade_blocked(
                conn, tip, {root}, max_depth=3
            ).state == box._rev.UNKNOWN_TRUNCATED
        finally:
            conn.close()


def test_T4_not_blocked_complete_walk_no_warning(capsys):
    with cascade_playbox() as box:
        tip, _root = _build_chain(box)
        conn = box._core._get_db()
        try:
            warnings: list = []
            capsys.readouterr()
            # no pending ancestors at all, and a pending set that holds only
            # the node itself (a node is never blocked by its own marker)
            assert box._rev._cascade_blocked(
                conn, tip, set(), warnings=warnings).state == box._rev.CLEAR
            assert box._rev._cascade_blocked(
                conn, tip, {tip}, warnings=warnings).state == box._rev.CLEAR
            assert warnings == []
            assert capsys.readouterr().err == ""
        finally:
            conn.close()


def test_T3_frontier_response_carries_the_warning_only_on_truncation(monkeypatch):
    """The frontier response surfaces ``cascade_walk_truncated`` when a
    blocked-check walk truncates, and omits it entirely otherwise."""
    with cascade_playbox() as box:
        tip, root = _build_chain(box)
        # real retract: markers land on d1..d5, so d1 is the frontier and
        # d5 (tip) is blocked on a full walk
        assert "error" not in box.retract(box["o0"])
        healthy = box.frontier()
        assert "cascade_walk_truncated" not in healthy

        # force a tiny budget for the blocked-check walk
        rev = box._rev
        real = rev._cascade_ancestors

        def tiny(conn, node_id, **kw):
            kw["max_depth"] = 2
            return real(conn, node_id, **kw)

        monkeypatch.setattr(rev, "_cascade_ancestors", tiny)
        truncated = box.frontier()
        assert "cascade_walk_truncated" in truncated
        assert isinstance(truncated["cascade_walk_truncated"], str)
        # truncation reads as blocked (safe direction): nothing past the
        # budget may be offered as frontier
        ids = {f["id"] for f in truncated["frontier"]}
        assert box.id_of(box["d5"]) not in ids


# ---------------------------------------------------------------------------
# T6 — diamond: the queue can end holding only already-visited duplicates
# ---------------------------------------------------------------------------


def _build_diamond(box):
    """tip <- {b, c} <- d (both b and c cite d): d is queued twice."""
    box.node("d", kind="observation")
    box.node("b", kind="derivation", supports=[box["d"]])
    box.node("c", kind="derivation", supports=[box["d"]])
    box.node("tip", kind="derivation", supports=[box["b"], box["c"]])
    return box.id_of(box["tip"]), box.id_of(box["d"])


@pytest.mark.parametrize("strict", [True, False])
def test_T6_diamond_duplicate_only_queue_is_complete(capsys, strict):
    with cascade_playbox() as box:
        tip, root = _build_diamond(box)
        conn = box._core._get_db()
        try:
            n = _full_size(box, conn, tip)
            # Walk order tip, b, c, d: after d is dequeued the queue holds
            # only d's second (already-visited) entry. A budget equal to the
            # distinct node count ends exactly there.
            assert n == 4
            capsys.readouterr()
            got = _walk(box, conn, tip, max_depth=n, strict=strict)
            assert len(got) == n
            assert capsys.readouterr().err == ""
        finally:
            conn.close()


def test_T6_cascade_blocked_diamond_true_answer_no_warning():
    with cascade_playbox() as box:
        tip, root = _build_diamond(box)
        conn = box._core._get_db()
        try:
            n = _full_size(box, conn, tip)
            w: list = []
            assert box._rev._cascade_blocked(
                conn, tip, {root}, warnings=w, max_depth=n
            ).state == box._rev.BLOCKED
            assert w == []
            w2: list = []
            assert box._rev._cascade_blocked(
                conn, tip, set(), warnings=w2, max_depth=n
            ).state == box._rev.CLEAR
            assert w2 == []
        finally:
            conn.close()


def test_T6_remaining_queue_counts_only_unvisited():
    with cascade_playbox() as box:
        tip, _root = _build_diamond(box)
        conn = box._core._get_db()
        try:
            # budget 2: tip, b dequeued; queue = [c, d]; both unvisited
            with pytest.raises(box._core.CascadeWalkTruncated) as ei:
                _walk(box, conn, tip, max_depth=2, strict=True)
            assert ei.value.remaining_queue == 2
            # budget 3: tip, b, c dequeued; queue = [d, d], both unvisited
            with pytest.raises(box._core.CascadeWalkTruncated) as ei:
                _walk(box, conn, tip, max_depth=3, strict=True)
            assert ei.value.remaining_queue == 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# T7 / T8 — the other two walks: resolve refusal, and mode C candidates
# ---------------------------------------------------------------------------


def _truncate_walk_for(monkeypatch, box, node_id, budget=2):
    """Give ONLY ``node_id``'s ancestor walk a tiny budget, so a field that
    appears in the response can only come from that walk."""
    rev = box._rev
    real = rev._cascade_ancestors
    seen: list = []

    def tiny(conn, nid, **kw):
        # Only the FIRST walk for node_id is truncated. In mode C that is the
        # candidate walk; the later blocked-check walk over the same node
        # runs at full budget, so it cannot supply the field by itself.
        if nid == node_id and not seen:
            seen.append(nid)
            kw["max_depth"] = budget
        return real(conn, nid, **kw)

    monkeypatch.setattr(rev, "_cascade_ancestors", tiny)


def test_T7_resolve_refusal_on_truncated_walk(monkeypatch, capsys):
    import json

    with cascade_playbox() as box:
        tip, _root = _build_chain(box)
        assert "error" not in box.retract(box["o0"])
        _truncate_walk_for(monkeypatch, box, tip)
        capsys.readouterr()
        out = json.loads(box._rev._cascade_resolve_impl(
            node_id=tip, resolution_kind="acknowledged"))
        err_lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
        assert "error" in out
        assert "cut short" in out["error"]
        assert "2" in out["error"]  # the budget is named
        # pin the field's CONTENT, not just its presence: node, budget,
        # visited and unvisited-queue counts (chain: budget 2 = tip + 1
        # premise, one unvisited premise still queued)
        text = out["cascade_walk_truncated"]
        assert tip in text
        assert "budget 2 exhausted after 2 node(s), 1 still queued" in text
        assert "of its cascade-ancestors" not in out["error"]  # no partial count
        assert "blocking_ancestors" not in out
        assert isinstance(out.get("cascade_walk_truncated"), str)
        assert "engram_cascade_frontier" not in out["next_call"]
        # one walk: no second non-strict walk, so no stderr truncation line
        assert not [l for l in err_lines if "_walk_cascade_upstream" in l]


def test_T7_control_untruncated_refusal_unchanged():
    import json

    with cascade_playbox() as box:
        tip, _root = _build_chain(box)
        assert "error" not in box.retract(box["o0"])
        out = json.loads(box._rev._cascade_resolve_impl(
            node_id=tip, resolution_kind="acknowledged"))
        assert "of its cascade-ancestors" in out["error"]
        assert out["blocking_ancestors"]
        assert "cascade_walk_truncated" not in out
        assert "engram_cascade_frontier" in out["next_call"]


def test_T8_mode_c_truncated_walk_carries_the_field(monkeypatch):
    with cascade_playbox() as box:
        tip, _root = _build_chain(box)
        assert "error" not in box.retract(box["o0"])
        healthy = box.frontier(box["d5"])
        assert "cascade_walk_truncated" not in healthy
        _truncate_walk_for(monkeypatch, box, tip)
        out = box.frontier(box["d5"])
        assert isinstance(out.get("cascade_walk_truncated"), str)
        assert "PARTIAL" in out["cascade_walk_truncated"]


def test_T8b_mode_c_falls_back_to_the_partial_candidate_set(monkeypatch):
    """Not just the flag: the fallback candidates are the walk's PARTIAL set
    intersected with pending (non-empty), not an empty set and not the full
    ancestor set. Observable as `pending_ancestor_count` (= candidates minus
    the node itself)."""
    with cascade_playbox() as box:
        tip, _root = _build_chain(box)
        assert "error" not in box.retract(box["o0"])
        healthy_count = box.frontier(box["d5"])["pending_ancestor_count"]

        conn = box._core._get_db()
        try:
            pending = {
                r["node_id"] for r in conn.execute(
                    "SELECT DISTINCT node_id FROM cascade_markers "
                    "WHERE status = 'pending'")
            }
            with pytest.raises(box._core.CascadeWalkTruncated) as ei:
                box._rev._cascade_ancestors(conn, tip, strict=True, max_depth=2)
            expected = (ei.value.partial & pending) - {tip}
        finally:
            conn.close()
        assert expected, "fixture: the partial set must hold a pending ancestor"
        assert len(expected) < healthy_count

        _truncate_walk_for(monkeypatch, box, tip)
        out = box.frontier(box["d5"])
        assert out["pending_ancestor_count"] == len(expected)


def test_blocked_result_refuses_truthiness():
    """`if _cascade_blocked(...)` would collapse three states into two."""
    with cascade_playbox() as box:
        tip, root = _build_chain(box)
        conn = box._core._get_db()
        try:
            r = box._rev._cascade_blocked(conn, tip, {root})
            with pytest.raises(TypeError):
                bool(r)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# T9 — one definition of "blocked": nobody else walks ancestors for it
# ---------------------------------------------------------------------------


def _calls_by_function(tree, callee_attr_or_name):
    """{enclosing function name: [Call nodes]} for calls to the named callee
    (bare name, or the attribute of ``module.name``)."""
    import ast

    found: dict = {}

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name == callee_attr_or_name:
                found.setdefault(self.stack[-1] if self.stack else "<module>", []).append(node)
            self.generic_visit(node)

    V().visit(tree)
    return found


def _revision_tree():
    import ast
    import engram_revision as rev_mod

    return ast.parse(Path(rev_mod.__file__).read_text(encoding="utf-8"))


def test_T9_ancestor_walks_live_only_in_cascade_blocked():
    """`_cascade_ancestors` is called from exactly two places in
    engram_revision: `_cascade_blocked` (THE blocked-decision) and
    `_cascade_frontier_impl` — once, for mode C's candidate LISTING
    (ancestors(node) intersected with pending). That one is a listing, not a
    blocked-decision, so it is the declared exception. Any other caller
    (e.g. the resolve refusal re-inlining its own walk) means two places
    decide what 'blocked' is. (Bound: a name-matching visitor; an aliased
    import or getattr would evade it. See T9c.)"""
    calls = _calls_by_function(_revision_tree(), "_cascade_ancestors")
    assert set(calls) == {"_cascade_blocked", "_cascade_frontier_impl"}, (
        f"_cascade_ancestors called from {sorted(calls)}; only "
        f"_cascade_blocked (the decision) and _cascade_frontier_impl "
        f"(mode C candidate listing) may call it"
    )
    assert len(calls["_cascade_frontier_impl"]) == 1  # the mode-C listing only
    assert len(calls["_cascade_blocked"]) == 1


def test_T9_raw_walker_only_behind_cascade_ancestors():
    calls = _calls_by_function(_revision_tree(), "_walk_cascade_upstream")
    assert set(calls) == {"_cascade_ancestors"}, sorted(calls)


def test_T9c_cascade_blocked_callers_are_exactly_the_three_known_functions():
    """The functions that call `_cascade_blocked` are exactly: the frontier
    impl, the resolve impl (refusal) and the auto-resolve function (the only
    caller that writes). A new caller or a lost one changes this set and
    must be a deliberate edit here, because each caller has to branch on the
    three-valued state.

    Bound: this is a name-matching AST visitor (bare names and ``mod.name``
    attributes). An aliased import (``from x import _cascade_ancestors as
    f``) or a ``getattr`` lookup would evade it, so T9 guards against the
    honest re-inlining mistake, not against a determined bypass."""
    calls = _calls_by_function(_revision_tree(), "_cascade_blocked")
    assert set(calls) == {
        "_cascade_frontier_impl",
        "_cascade_resolve_impl",
        "_cascade_auto_resolve_trivial",
    }, sorted(calls)


# ---------------------------------------------------------------------------
# T10 — the auto-resolve loop is the one caller that WRITES
# ---------------------------------------------------------------------------


def test_T10_auto_resolve_leaves_marker_pending_when_walk_truncates():
    """Out-of-order state (invariant 6 not holding): R1 <- G <- P1 <- N,
    R1 superseded so G, P1, N all carry pending markers; P1's marker is then
    cleared out of order while G stays pending. N's direct premise P1 is
    resolved 'acknowledged', so EVERY later auto-resolve check would pass —
    only the blocked-decision stands between N and a silent resolution. With
    N's ancestor walk cut short (UNKNOWN_TRUNCATED) the loop must not write."""
    import datetime

    with cascade_playbox() as box:
        box.node("r1", kind="observation")
        box.node("r1b", kind="observation")
        box.node("g", kind="derivation", supports=[box["r1"]])
        box.node("p1", kind="derivation", supports=[box["g"]])
        box.node("n", kind="derivation", supports=[box["p1"]])
        assert "error" not in box.supersede(box["r1"], box["r1b"])
        g, p1, n = (box.id_of(box[x]) for x in ("g", "p1", "n"))
        rev, core = box._rev, box._core
        conn = core._get_db()
        try:
            def pending(nid):
                return conn.execute(
                    "SELECT COUNT(*) AS c FROM cascade_markers "
                    "WHERE node_id = ? AND status = 'pending'", (nid,)
                ).fetchone()["c"]

            assert pending(g) == pending(p1) == pending(n) == 1
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            rev._resolve_own_markers(conn, p1, "acknowledged", now)
            assert pending(p1) == 0 and pending(g) == 1

            # control: full budget -> N is BLOCKED by G, nothing auto-resolves
            assert rev._cascade_auto_resolve_trivial(conn, p1, now) == []
            assert pending(n) == 1

            # truncated: only N's walk is cut short (budget 2 = n, p1)
            real = rev._cascade_ancestors

            def tiny(c, nid, **kw):
                if nid == n:
                    kw["max_depth"] = 2
                return real(c, nid, **kw)

            rev._cascade_ancestors = tiny
            try:
                assert rev._cascade_auto_resolve_trivial(conn, p1, now) == []
            finally:
                rev._cascade_ancestors = real
            assert pending(n) == 1, "truncated walk must never auto-resolve"
        finally:
            conn.close()
