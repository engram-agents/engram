"""#4823: the downstream cascade walk must not recurse once per derivation level.

Contract (Kepler 2026-11-15, design owner; cells as ruled by Borges, design seat):
  (a) ORDER IDENTITY: the same on_live_node SEQUENCE as the recursive walk it replaced,
      on generated DAGs with diamonds, shared dependents, a cycle, a non-claim-bearing
      node, a non-current node, and a callback that writes new edges as it runs. The
      recursive walk is kept below as a test-only reference.
  (b) DEPTH: a chain of max(5000, 3 x the default recursion limit) completes under the
      DEFAULT limit in a live retraction and in migration rung 11, marking every level.
  (c) a cycle terminates, each node handed to on_live_node once.
Cell 0 is the control for (b): the reference walk really fails on that chain.
Mutants that must redden (a) and leave (b) green, listed in the PR: postorder (callback
after the children), reversed child order, visited check moved after on_live_node.
"""
import os
import random
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_retract_backfill_4727 as rb  # noqa: E402  (the #4727 store builders)

DEEP = max(5000, 3 * sys.getrecursionlimit())


def _reference_walk(core, conn, node_id, *, visited, on_live_node):
    """Test-only reference: the walk exactly as it was before #4823 (recursive preorder)."""
    if node_id in visited:
        return
    visited.add(node_id)
    node = conn.execute(
        "SELECT id, type, claim, metadata FROM nodes WHERE id = ? AND is_current = 1",
        (node_id,),
    ).fetchone()
    if not node:
        return
    if not core._is_claim_bearing(node["type"]):
        return
    on_live_node(node)
    for dep_id, _t, _r in core.dag_edge_set(
            conn, core._CASCADE_RELATIONS_SORTED, target_id=node_id):
        _reference_walk(core, conn, dep_id, visited=visited, on_live_node=on_live_node)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    d = tempfile.mkdtemp(prefix="engram_test_4823_")
    rev, obs, derive, lifecycle, core = rb._mods(d)
    try:
        yield dict(rev=rev, obs=obs, derive=derive, core=core, dir=d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _clone(core, template_id, ids):
    """Insert claim-bearing rows copied from one real derivation (fast bulk build)."""
    c = rb._raw(core)
    try:
        row = c.execute("SELECT * FROM nodes WHERE id = ?", (template_id,)).fetchone()
        cols = row.keys()
        for nid in ids:
            vals = [nid if k == "id" else row[k] for k in cols]
            c.execute(f"INSERT INTO nodes ({','.join(cols)}) "
                      f"VALUES ({','.join('?' * len(cols))})", vals)
        c.commit()
    finally:
        c.close()


def _edges(conn_or_core, pairs, relation="derives_from"):
    """pairs of (dependent, premise): dependent derives_from premise."""
    own = not isinstance(conn_or_core, sqlite3.Connection)
    c = rb._raw(conn_or_core) if own else conn_or_core
    try:
        for src, tgt in pairs:
            c.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                      "VALUES (?, ?, ?, '2026-11-15T00:00:00+00:00')", (src, tgt, relation))
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def _memory_copy(core):
    """A private in-memory copy of the store, so a writing callback in one walk cannot
    change the graph the other walk sees."""
    src = rb._raw(core)
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst)
    finally:
        src.close()
    dst.row_factory = sqlite3.Row
    return dst


def _sequence(core, walk, start, *, writer=None):
    c = _memory_copy(core)
    try:
        seq = []

        def on_live(node):
            seq.append(node["id"])
            if writer:
                writer(c, node["id"])

        walk(c, start, visited=set(), on_live_node=on_live)
        return seq
    finally:
        c.close()


def _reference(core):
    return lambda c, n, **k: _reference_walk(core, c, n, **k)


def _chain(s, depth):
    """root observation <- level 1 (a real derivation) <- ... <- level `depth`."""
    core = s["core"]
    root = rb._ob(s["obs"], "deep-chain root premise")
    first = rb._dv(s["derive"], "level 1 rests on root", [root])
    rest = [f"dv_deep_{i:05d}" for i in range(2, depth + 1)]
    _clone(core, first, rest)
    levels = [first] + rest
    _edges(core, list(zip(levels[1:], levels[:-1])))
    return root, levels


def _generated_dag(s, seed, n=40):
    """A random layered DAG over n claim-bearing nodes under one root derivation: each
    node derives from 1-3 earlier nodes (diamonds, shared dependents), plus one back edge
    (a cycle), one non-claim-bearing dependent and one non-current node with a dependent
    behind it. Returns (start, spare ids for the writing callback)."""
    core = s["core"]
    rng = random.Random(seed)
    R = rb._ob(s["obs"], f"generated root {seed}")
    first = rb._dv(s["derive"], f"generated level 0, seed {seed}", [R])
    ids = [first] + [f"dv_gen{seed}_{i:03d}" for i in range(1, n)]
    spare = [f"dv_gen{seed}_spare_{i:02d}" for i in range(8)]
    _clone(core, first, ids[1:] + spare)
    pairs = []
    for i in range(1, n):
        for p in rng.sample(range(i), k=min(i, rng.randint(1, 3))):
            pairs.append((ids[i], ids[p]))
    lo, hi = sorted(rng.sample(range(1, n), 2))
    pairs.append((ids[lo], ids[hi]))  # back edge: a cycle through lo..hi
    _edges(core, pairs)
    c = rb._raw(core)
    try:
        c.execute("INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
                  "VALUES (?, 'feeling_report', '2026-11-15T00:00:00+00:00', 'ncb', 'active', 1)",
                  (f"fl_gen{seed}_ncb",))
        dead = ids[rng.randrange(1, n)]
        c.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (dead,))
        c.commit()
    finally:
        c.close()
    _edges(core, [(f"fl_gen{seed}_ncb", ids[rng.randrange(n)])])
    return first, spare, dead


# ---------------------------------------------------------------------------

def test_cell0_control_the_reference_walk_fails_on_the_deep_chain(store):
    core = store["core"]
    _root, levels = _chain(store, DEEP)
    c = rb._raw(core)
    try:
        with pytest.raises(RecursionError):
            _reference_walk(core, c, levels[0], visited=set(), on_live_node=lambda n: None)
    finally:
        c.close()


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_cell_a_order_identity_on_generated_dags(store, seed):
    core = store["core"]
    start, spare, dead = _generated_dag(store, seed)
    rec = _sequence(core, _reference(core), start)
    cur = _sequence(core, core._walk_cascade_downstream, start)
    assert cur == rec, f"seed {seed}: on_live_node sequence differs from the reference"
    assert len(rec) > 10 and dead not in rec, "fixture: graph not reached, or the dead node fired"

    # A callback that writes: on every fourth live node, add a spare node as a NEW
    # dependent of it. The reference builds each child list after the callback, so the
    # spare is walked; an implementation that built it earlier would miss or move it.
    rec_w = _sequence(core, _reference(core), start, writer=_fresh_writer(spare))
    cur_w = _sequence(core, core._walk_cascade_downstream, start, writer=_fresh_writer(spare))
    assert cur_w == rec_w, f"seed {seed}: sequence differs under a writing callback"
    assert set(spare) & set(rec_w), "fixture: the writing callback added nothing that was walked"


def _fresh_writer(spare):
    state = {"n": 0, "spare": list(spare)}

    def writer(c, nid):
        state["n"] += 1
        if state["n"] % 4 == 0 and state["spare"]:
            _edges(c, [(state["spare"].pop(0), nid)])
    return writer


def test_cell_b1_live_retraction_completes_on_a_deep_chain(store):
    assert sys.getrecursionlimit() < DEEP, "the chain must be deeper than the limit"
    core = store["core"]
    root, levels = _chain(store, DEEP)
    rb._retract(store["rev"], root)
    assert set(rb._retract_marked(core, root)) == set(levels)


def test_cell_b2_rung_11_completes_on_a_deep_chain(store, capsys):
    core = store["core"]
    root, levels = _chain(store, DEEP)
    rb._retract(store["rev"], root)
    rb._forget_retraction_markers(core, root)
    rb._set_version(core, rb.RUNG - 1)
    core._db_setup_done_paths.clear()
    capsys.readouterr()
    assert rb._ladder(core) == rb.RUNG, capsys.readouterr().err
    assert set(rb._retract_marked(core, root)) == set(levels)


def test_cell_c_a_cycle_terminates_and_each_node_fires_once(store):
    core, obs, derive = store["core"], store["obs"], store["derive"]
    R = rb._ob(obs, "R: the premise under the cycle.")
    A = rb._dv(derive, "A rests on R.", [R])
    B = rb._dv(derive, "B rests on A.", [A])
    C = rb._dv(derive, "C rests on B.", [B])
    _edges(core, [(A, C)])  # closes A <- B <- C <- A
    assert _sequence(core, core._walk_cascade_downstream, A) == [A, B, C]
    assert _sequence(core, _reference(core), A) == [A, B, C]
