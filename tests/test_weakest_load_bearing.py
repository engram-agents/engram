"""Tests for tools/weakest_load_bearing.py -- the read-only "weakest load-bearing
beliefs" ranking (engram-alpha#3921, Calibration step 4).

The instrument is a pure function over an already-open sqlite3 connection:
``rank(conn, n=5, current_turn=None) -> list[dict]``.  score = load x weakness;
load = distinct transitive CURRENT dependents along the one-hop premise relations
(core.CITATION_SUPPORT_RELATIONS); weakness = 1 - confidence, or 1.0 when the node
is contested / stale / has NULL confidence.

Every cell builds its own graph on a copy of a REAL schema (created once through
the real server's _get_db, never a hand-rolled CREATE TABLE, so the fixture cannot
drift from the nodes/edges/cascade_markers tables) and inserts rows with raw SQL.
Each cell's docstring names the mutation that must redden it.

Fixture filler: ten live derivations at confidence 0.8, each with TWO dependents
(score 0.4 each, importance 0.9), so top-5 is a real competition.  The node under
test carries importance_score 0.05 so that a score built on importance, not
weakness, cannot rank it.
"""

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util  # noqa: E402

# Loaded by path (like the other read-only report tools' tests): the repo-root
# tools/ mirror carries only symlinks for modules that need one, and this module
# does not.
_TOOL_PATH = (Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
              / "weakest_load_bearing.py")
_spec = importlib.util.spec_from_file_location("weakest_load_bearing", _TOOL_PATH)
_wlb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wlb)
format_text, rank, to_json = _wlb.format_text, _wlb.rank, _wlb.to_json

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_weakest_load_bearing_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TEMPLATE = {}


def _template_db() -> Path:
    """One real-schema, empty knowledge.db, built once through the real server."""
    if "path" in _TEMPLATE:
        return _TEMPLATE["path"]
    home = Path(TEST_DIR) / "template_home"
    shutil.rmtree(home, ignore_errors=True)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    import server

    server._configure_paths(str(home))
    server._ensure_data_dir()
    conn = server._get_db()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    _TEMPLATE["path"] = home / "knowledge.db"
    return _TEMPLATE["path"]


class Graph:
    """A fixture graph on a private copy of the real schema."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "fixture.db"
        shutil.copyfile(_template_db(), self.path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._n = 0

    def node(self, nid, ntype="derivation", conf=0.8, *, claim=None, current=1,
             status="active", importance=0.05, recall_turn=0, quote_type=None,
             evidence_id=None, source_url=None, source_title=None):
        self.conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, confidence, is_current, "
            "status, importance_score, recall_turn, quote_type, evidence_id, "
            "source_url, source_title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, ntype, claim if claim is not None else f"claim of {nid}",
             "2026-01-01T00:00:00", conf, current, status, importance, recall_turn,
             quote_type, evidence_id, source_url, source_title),
        )
        return nid

    def edge(self, src, tgt, rel="derives_from"):
        self.conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?,?,?,?)", (src, tgt, rel, "2026-01-01T00:00:00"))

    def dependents(self, target, k, *, prefix=None, rel="derives_from", **kw):
        prefix = prefix or f"d_{target}"
        ids = []
        for i in range(k):
            did = f"{prefix}_{i}"
            self.node(did, conf=kw.get("conf", 0.9), importance=0.9)
            self.edge(did, target, rel)
            ids.append(did)
        return ids

    def marker(self, node_id, status="pending", root="rootX", event="ev1"):
        self.conn.execute(
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, "
            "status, created_at) VALUES (?,?,?,?,?,?)",
            (node_id, event, root, "supersede", status, "2026-01-02T00:00:00"))

    def contradict(self, target, status="active", cid=None):
        cid = cid or f"ct_{target}"
        self.node(cid, ntype="contradiction", conf=None, status=status)
        self.edge(cid, target, "contradicts")
        return cid

    def filler(self):
        """Ten 0.8-confidence nodes, two dependents each: score 0.4 apiece."""
        for i in range(10):
            fid = f"fill_{i:02d}"
            self.node(fid, conf=0.8, importance=0.9)
            self.dependents(fid, 2, prefix=f"fd_{i:02d}")

    def commit(self):
        self.conn.commit()

    def ids(self, n=5, **kw):
        self.commit()
        return [r["id"] for r in rank(self.conn, n=n, **kw)]

    def row(self, nid, n=100, **kw):
        self.commit()
        for r in rank(self.conn, n=n, **kw):
            if r["id"] == nid:
                return r
        return None


@pytest.fixture
def g(tmp_path):
    graph = Graph(tmp_path)
    yield graph
    graph.conn.close()


# --------------------------------------------------------------------------
# (a) the basic weak-and-load-bearing case
# --------------------------------------------------------------------------
def test_a_weak_node_with_three_dependents_ranks_top5(g):
    """(a) conf 0.3 + 3 dependents -> score 2.1, top 5.
    Mutation 6 (importance instead of weakness) reddens it: importance 0.05."""
    g.filler()
    g.node("weak", conf=0.3)
    g.dependents("weak", 3)
    assert "weak" in g.ids(5)
    r = g.row("weak")
    assert r["load"] == 3
    assert r["weakness"] == pytest.approx(0.7)
    assert r["score"] == pytest.approx(2.1)
    assert r["weakness_reason"] is None


# (b) axiom / lesson / cornerstone excluded
def test_b_non_claim_bearing_with_many_dependents_absent(g):
    """(b) an axiom with 100 dependents never appears; neither do a lesson or a
    cornerstone (NULL confidence would otherwise read as weakness 1.0).
    Mutation 5 (include axioms) reddens it: the axiom is given confidence 0.2."""
    g.filler()
    g.node("ax_fix", ntype="axiom", conf=0.2)
    g.dependents("ax_fix", 100)
    g.node("ls_fix", ntype="lesson", conf=None)
    g.dependents("ls_fix", 5)
    g.node("cs_fix", ntype="cornerstone", conf=None)
    g.dependents("cs_fix", 5)
    got = g.ids(n=1000)
    assert "ax_fix" not in got
    assert "ls_fix" not in got
    assert "cs_fix" not in got


# (c) strong + no dependents
def test_c_high_confidence_zero_dependents_absent(g):
    """(c) 0.95 with zero dependents does not appear (load 0)."""
    g.filler()
    g.node("strong", conf=0.95)
    g.node("lonely_weak", conf=0.05)  # weak but nothing leans on it
    got = g.ids(n=1000)
    assert "strong" not in got
    assert "lonely_weak" not in got


# (d) contested override
def test_d_contested_high_confidence_ranks_top5(g):
    """(d) conf 0.9 + active contradiction + 3 dependents: weakness 1.0, score 3.0.
    Without the override it would score 0.3 < the 0.4 filler and fall out.
    Mutation 1 (drop contested override) reddens it."""
    g.filler()
    g.node("contested", conf=0.9)
    g.dependents("contested", 3)
    g.contradict("contested")
    assert "contested" in g.ids(5)
    r = g.row("contested")
    assert r["weakness"] == 1.0
    assert r["weakness_reason"] == "contested"


# (e) stale override
def test_e_pending_marker_overrides_resolved_does_not(g):
    """(e) conf 0.9 + pending cascade marker + 3 dependents ranks top 5; the same
    node with the marker 'resolved' keeps weakness 0.1.
    Mutation 2 (drop stale override) reddens the pending half."""
    g.filler()
    g.node("stale", conf=0.9)
    g.dependents("stale", 3)
    g.marker("stale", status="pending")
    assert "stale" in g.ids(5)
    r = g.row("stale")
    assert r["weakness"] == 1.0 and r["weakness_reason"] == "stale"

    g.conn.execute("UPDATE cascade_markers SET status='resolved' WHERE node_id='stale'")
    g.commit()
    r2 = g.row("stale")
    assert r2["weakness"] == pytest.approx(0.1)
    assert r2["weakness_reason"] is None
    assert "stale" not in g.ids(5)


# (f) resolved contradiction
def test_f_resolved_contradiction_no_override(g):
    """(f) a contradiction whose status left 'active' triggers nothing; a
    non-current contradiction triggers nothing either."""
    g.filler()
    g.node("was_contested", conf=0.9)
    g.dependents("was_contested", 3)
    g.contradict("was_contested", status="resolved")
    r = g.row("was_contested")
    assert r["weakness"] == pytest.approx(0.1)
    assert r["weakness_reason"] is None
    assert "was_contested" not in g.ids(5)


def test_f2_partially_resolved_contradiction_still_contests(g):
    """A partially_resolved contradiction is still unsettled
    (core.UNSETTLED_CONTRADICTION_STATUSES) and keeps the override."""
    g.node("half", conf=0.9)
    g.dependents("half", 3)
    g.contradict("half", status="partially_resolved")
    assert g.row("half")["weakness_reason"] == "contested"


# (g) transitive + dedup
def test_g_diamond_counts_each_dependent_once(g):
    """(g) X <- A, X <- B, A <- C, B <- C: load(X) = 3 (A, B, C), not 4 and not 2.
    Mutation 3 (direct only) gives 2; mutation 4 (no dedup) gives 4."""
    g.node("X", conf=0.5)
    for d in ("A", "B", "C"):
        g.node(d, conf=0.9)
    g.edge("A", "X")
    g.edge("B", "X")
    g.edge("C", "A")
    g.edge("C", "B")
    assert g.row("X")["load"] == 3


def test_g2_chain_is_transitive_and_cycles_terminate(g):
    """A chain counts every hop; a cycle neither loops forever nor makes a node
    its own dependent."""
    g.node("X", conf=0.5)
    for d in ("A", "B", "C"):
        g.node(d, conf=0.9)
    g.edge("A", "X")
    g.edge("B", "A")
    g.edge("C", "B")
    g.edge("X", "C")  # cycle back to X
    r = g.row("X")
    assert r["load"] == 3  # A, B, C -- not X itself


def test_g3_only_premise_relations_conduct(g):
    """Only core.CITATION_SUPPORT_RELATIONS edges make a dependent: 'cites' (and
    any other relation) does not; all three premise relations do."""
    g.node("X", conf=0.5)
    for i, rel in enumerate(("derives_from", "supported_by", "acknowledged_derives_from")):
        g.node(f"p{i}", conf=0.9)
        g.edge(f"p{i}", "X", rel)
    g.node("c1", conf=0.9)
    g.edge("c1", "X", "cites")
    g.node("c2", conf=0.9)
    g.edge("c2", "X", "relates_to")
    assert g.row("X")["load"] == 3


# (h) non-current / retracted dependents
def test_h_non_current_and_retracted_dependents_not_counted(g):
    """(h) is_current=0 and retracted dependents do not count, and neither does a
    dependent reachable only THROUGH one.
    Mutation 7 (count non-current) reddens it."""
    g.node("X", conf=0.5)
    g.dependents("X", 1, prefix="live")
    g.node("old1", conf=0.9, current=0)
    g.edge("old1", "X")
    g.node("old2", conf=0.9, current=0)
    g.edge("old2", "X")
    g.node("dead1", conf=0.9, status="retracted")
    g.edge("dead1", "X")
    g.node("dead2", conf=0.9, status="retracted")
    g.edge("dead2", "X")
    g.node("behind_old", conf=0.9)
    g.edge("behind_old", "old1")  # only route to X is through a non-current node
    assert g.row("X")["load"] == 1


def test_h2_non_current_or_retracted_target_never_ranks(g):
    """A superseded or retracted X is not a belief to study even with dependents."""
    g.node("super", conf=0.1, current=0)
    g.dependents("super", 3)
    g.node("retr", conf=0.1, status="retracted")
    g.dependents("retr", 3, prefix="rd")
    got = g.ids(n=1000)
    assert "super" not in got and "retr" not in got


# (i) read-only
def test_i_database_unchanged_after_run(g):
    """(i) the DB file bytes and a full row dump are identical before and after
    ranking (no recall/utility bump, no write of any kind)."""
    g.filler()
    g.node("weak", conf=0.3, claim="line one\nline two")
    g.dependents("weak", 3)
    g.contradict("weak")
    g.marker("weak")
    g.commit()
    g.conn.close()

    def dump(path):
        c = sqlite3.connect(str(path))
        try:
            return {
                t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("nodes", "edges", "cascade_markers")
            }
        finally:
            c.close()

    before_bytes = g.path.read_bytes()
    before_rows = dump(g.path)
    conn = sqlite3.connect(f"file:{g.path}?mode=ro", uri=True)
    try:
        rows = rank(conn, n=20, current_turn=50)
        assert rows  # the run really did something
        text = format_text(rows)
        assert text
        to_json(rows)
    finally:
        conn.close()
    assert g.path.read_bytes() == before_bytes
    assert dump(g.path) == before_rows
    # reopen so the fixture teardown has a connection to close
    g.conn = sqlite3.connect(str(g.path))


# (j) determinism
def test_j_equal_scores_order_by_id(g):
    """(j) equal scores order by id ascending, regardless of insertion order.
    Mutation: ordering by something else (e.g. reverse id, insertion) reddens it."""
    for nid in ("zeta", "alpha", "mid"):
        g.node(nid, conf=0.5)
        g.dependents(nid, 2, prefix=f"dd_{nid}")
    assert g.ids(n=10) == ["alpha", "mid", "zeta"]


def test_j2_float_noise_does_not_break_the_tie(g):
    """0.8 x 3 and 0.6 x 4 are the same score up to float noise; id decides."""
    g.node("b_node", conf=0.2)  # weakness 0.8
    g.dependents("b_node", 3, prefix="bd")
    g.node("a_node", conf=0.4)  # weakness 0.6
    g.dependents("a_node", 4, prefix="ad")
    assert g.ids(n=10) == ["a_node", "b_node"]


# --------------------------------------------------------------------------
# NULL confidence, output fields, falsifiers, n
# --------------------------------------------------------------------------
@pytest.mark.parametrize("older", ["a_node", "b_node"])
def test_j3_recency_and_importance_do_not_enter_the_score(g, older):
    """Two nodes identical in confidence and dependents, but with very different
    recall_turn AND importance, score IDENTICALLY and order only by the id tie-break
    -- whichever of the two is the older one.  current_turn is passed so
    turns_since_recall really differs (60 vs 0 would show if it leaked).
    Mutations that redden it: score *= 1 + turns_since_recall/100; weakness += a
    recency term; ordering on importance."""
    for nid in ("a_node", "b_node"):
        is_old = nid == older
        g.node(nid, conf=0.5, recall_turn=40 if is_old else 100,
               importance=0.99 if is_old else 0.01)
        g.dependents(nid, 2, prefix=f"rd_{nid}")
    g.commit()
    rows = rank(g.conn, n=10, current_turn=100)
    assert [r["id"] for r in rows] == ["a_node", "b_node"]
    assert {r["id"]: r["turns_since_recall"] for r in rows} == (
        {"a_node": 60, "b_node": 0} if older == "a_node" else {"a_node": 0, "b_node": 60}
    )
    assert rows[0]["score"] == rows[1]["score"] == pytest.approx(1.0)
    assert rows[0]["weakness"] == rows[1]["weakness"] == pytest.approx(0.5)


def test_k_null_confidence_claim_bearing_is_weakness_one(g):
    """NULL confidence on a claim-bearing node: weakness 1.0, reason
    'null_confidence', neither crash nor silent skip."""
    g.filler()
    g.node("nullconf", conf=None)
    g.dependents("nullconf", 3)
    r = g.row("nullconf")
    assert r is not None
    assert r["weakness"] == 1.0
    assert r["weakness_reason"] == "null_confidence"
    assert r["confidence"] is None
    assert "nullconf" in g.ids(5)


def test_l_n_limits_rows_and_default_is_five(g):
    g.filler()
    assert len(g.ids()) == 5
    assert len(g.ids(n=3)) == 3
    assert len(g.ids(n=1)) == 1


@pytest.mark.parametrize("bad_n", [0, -1, -100])
def test_l2_rank_rejects_n_below_one(g, bad_n):
    """n < 1 is a caller error, not 'nothing found': an empty list would read as a
    statement about the graph.  Mutation: `if n <= 0: return []` reddens it."""
    g.filler()
    g.commit()
    with pytest.raises(ValueError, match="n"):
        rank(g.conn, n=bad_n)


def test_m_claim_one_liner_collapses_newlines_and_truncates(g):
    g.node("long", conf=0.3, claim="alpha\nbeta\n\ngamma " + "x" * 300)
    g.dependents("long", 1)
    c = g.row("long")["claim"]
    assert "\n" not in c
    assert len(c) <= 120
    assert c.startswith("alpha beta gamma")


def test_n_turns_since_recall_is_turns_not_days(g):
    """turns = current_turn - recall_turn; unknown current turn -> None; text says
    'turns' and never 'days'."""
    g.node("w", conf=0.3, recall_turn=40)
    g.dependents("w", 1)
    assert g.row("w", current_turn=100)["turns_since_recall"] == 60
    assert g.row("w")["turns_since_recall"] is None
    g.commit()
    text = format_text(rank(g.conn, current_turn=100))
    assert "60 turns" in text
    assert "day" not in text.lower()


def test_o_falsifier_observation_and_hard_data(g):
    g.node("ev_1", ntype="evidence", conf=None, source_url="https://example.com/s",
           source_title="Source Title")
    g.node("obs_plain", ntype="observation_factual", conf=0.6, quote_type="official_statement",
           evidence_id="ev_1")
    g.dependents("obs_plain", 1)
    g.node("obs_hard", ntype="observation_factual", conf=0.6, quote_type="hard_data",
           evidence_id="ev_1")
    g.dependents("obs_hard", 1)
    plain = g.row("obs_plain")["falsifier"]
    assert "re-read the source" in plain
    assert "https://example.com/s" in plain
    hard = g.row("obs_hard")["falsifier"]
    # hard_data covers one-time observed events that cannot be re-run, so the wording
    # asks for a re-read first and a re-run only when the measurement is repeatable.
    assert "re-read the recorded measurement; re-run it if it is repeatable" in hard
    assert "re-run the measurement" not in hard
    assert "re-read the source" not in hard
    assert "https://example.com/s" in hard  # the evidence suffix is kept


def test_p_falsifier_derivation_weakest_premise_and_stale_successor(g):
    g.node("p_hi", conf=0.9)
    g.node("p_lo", conf=0.4)
    g.node("deriv", conf=0.6)
    g.edge("deriv", "p_hi")
    g.edge("deriv", "p_lo", "supported_by")
    g.dependents("deriv", 1)
    f = g.row("deriv")["falsifier"]
    assert "re-check the weakest premise" in f and "p_lo" in f and "p_hi" not in f

    g.node("moved", conf=0.9)
    g.node("deriv2", conf=0.9)
    g.edge("deriv2", "moved")
    g.edge("deriv2", "p_hi")
    g.dependents("deriv2", 1)
    g.marker("deriv2", root="moved", event="evS")
    g.marker("moved", status="pending", root="moved", event="evS")
    r = g.row("deriv2")
    assert r["weakness_reason"] == "stale"
    assert "check the premise's successor" in r["falsifier"]
    assert "moved" in r["falsifier"]


def test_q_conjecture_stale_uses_successor_wording(g):
    g.node("pm", conf=0.9)
    g.node("conj", ntype="conjecture", conf=0.4)
    g.edge("conj", "pm")
    g.dependents("conj", 1)
    g.marker("conj", root="pm", event="evC")
    g.marker("pm", root="pm", event="evC")
    assert "check the premise's successor" in g.row("conj")["falsifier"]


def test_r_json_shape(g):
    g.node("w", conf=0.3)
    g.dependents("w", 1)
    g.commit()
    import json

    data = json.loads(to_json(rank(g.conn, current_turn=10)))
    assert data[0]["id"] == "w"
    for key in ("id", "claim", "confidence", "load", "weakness", "weakness_reason",
                "turns_since_recall", "falsifier", "score", "type"):
        assert key in data[0]


def test_s_core_source_has_no_connect_line():
    """The core must be a pure function over a passed-in connection: no
    sqlite3.connect line in the module (the #2652 ledger counts them)."""
    src = (Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
           / "weakest_load_bearing.py").read_text()
    assert "sqlite3.connect(" not in src


# ==========================================================================
# Phase 2 -- the entry layer (engram-alpha#3921): reads ONLY through the shared
# owner-read helper (tools/_owner_read.py), behind
# `engramd.weakest_load_bearing_reads_via_owner` (default OFF).
#
# The §5.5 cells mirror tests/test_cohort_owner_read_3862.py (its C2 cells: owner
# down, every other owner failure, malformed rows, the CLI cells) and the tool
# wiring in tools/cohort_dispatch.py (OWNER_FLAG_KEY / CohortOwnerReadError /
# _owner_spec / _fetch_nodes).  Each cell's docstring names the mutation that must
# redden it:
#   (i)   fall back to a direct open when the owner is down
#   (ii)  return [] instead of raising on a malformed answer
#   (iii) ignore the flag (always the owner)
#   (iv)  current_turn not passed
# ==========================================================================
import hashlib  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner, registered_sql  # noqa: E402

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_read_registry as rr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FLAG_KEY = "weakest_load_bearing_reads_via_owner"
SITE_PREFIX = "tools/weakest_load_bearing.py:"
NEVER_EMPTY = "empty list"


@pytest.fixture(autouse=True)
def _restore_imports():
    _template_db()  # the one-time heavy import happens before the snapshot, so it is not unwound
    mods, path = dict(sys.modules), list(sys.path)
    yield
    for name in [n for n in sys.modules if n not in mods]:
        del sys.modules[name]
    for name, mod in mods.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
    sys.path[:] = path


def _load_tool(home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("weakest_load_bearing_p2", str(_TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _set_flag(home, value=True, turn=None):
    cfg = {"engramd": {FLAG_KEY: value}}
    if turn is not None:
        cfg["memory"] = {"current_turn": turn}
    (Path(home) / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _rich_graph(g):
    """Every query the tool sends has something to find: a plain weak node, a contested
    one, a stale one with a marked premise, a derivation with scored premises, and
    observations (plain and hard_data) with evidence."""
    g.filler()
    g.node("weak", conf=0.3, claim="line one\nline two", recall_turn=40)
    g.dependents("weak", 3)
    g.node("contested", conf=0.9, recall_turn=10)
    g.dependents("contested", 3)
    g.contradict("contested")
    g.node("moved", conf=0.9)
    g.node("stale", conf=0.9, recall_turn=25)
    g.edge("stale", "moved")
    g.dependents("stale", 3)
    g.marker("stale", root="moved", event="evS")
    g.marker("moved", root="moved", event="evS")
    g.node("p_hi", conf=0.9)
    g.node("p_lo", conf=0.4)
    g.node("deriv", conf=0.5, recall_turn=5)
    g.edge("deriv", "p_hi")
    g.edge("deriv", "p_lo", "supported_by")
    g.dependents("deriv", 3)
    g.node("evd", ntype="evidence", conf=None, source_url="https://example.com/s",
           source_title="Source Title")
    g.node("obs_hard", ntype="observation_factual", conf=0.4, quote_type="hard_data",
           evidence_id="evd", recall_turn=7)
    g.dependents("obs_hard", 3)
    g.node("obs_plain", ntype="observation_factual", conf=0.4, quote_type="official_statement",
           evidence_id="evd")
    g.dependents("obs_plain", 3)


@pytest.fixture
def seat(tmp_path):
    graph = Graph(tmp_path)
    _rich_graph(graph)
    graph.commit()
    graph.conn.close()
    # a short path: the owner socket lives at <home>/run/engramd.sock and AF_UNIX paths are
    # capped (104 bytes on macOS), which pytest's tmp_path can exceed
    home = Path(tempfile.mkdtemp(prefix="wlb-"))
    shutil.copyfile(graph.path, home / "knowledge.db")
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


class Opens:
    """Raise-and-record every way THIS thread could open a sqlite store (the in-process fake
    owner serves from other threads and keeps its own connections)."""

    def __init__(self):
        self.record = []
        self._thread = threading.get_ident()

    def install(self, monkeypatch):
        real = {"connect": sqlite3.connect, "dbapi2": sqlite3.dbapi2.connect}

        def guard(name):
            def f(*a, **k):
                if threading.get_ident() != self._thread:
                    return real[name](*a, **k)
                self.record.append((name, str(a[0]) if a else ""))
                raise AssertionError(f"direct open via {name}: {a[:1]}")
            return f

        monkeypatch.setattr(sqlite3, "connect", guard("connect"))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", guard("dbapi2"))
        return self


def _cli(home, args, **extra_env):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env.update(extra_env)
    return subprocess.run([sys.executable, str(_TOOL_PATH), *args], capture_output=True,
                          text=True, env=env, timeout=120)


# ---- registration: the statements the tool sends are the statements the scanner sees --------

def _sites():
    data = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    return [(t["sql"], s) for t in data["templates"] for s in t["sites"] if s.startswith(SITE_PREFIX)]


def test_p2_t1_the_tool_has_registered_templates_under_its_own_sites():
    """The templates are in the committed list from day one, each keyed to this file."""
    sites = _sites()
    assert sites, "no template is registered under tools/weakest_load_bearing.py"
    for sql, _site in sites:
        assert sql.startswith("SELECT")


def test_p2_t2_the_scan_config_names_the_tool_as_a_root():
    cfg = json.loads((REPO_ROOT / "tools" / "engramd_read_scan.config.json").read_text())
    assert any(r.startswith(SITE_PREFIX) for r in cfg["roots"]), "no scanner root for the tool"


def test_p2_t3_every_statement_a_full_run_sends_is_a_registered_template(seat, monkeypatch):
    """A run through a fake owner that records requests: every `read` SQL is one of this
    tool's registered templates, byte for byte (the owner admits exact bytes)."""
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    mine = {sql for sql, _s in _sites()}
    with FakeOwner(seat) as owner:
        rows = mod.read_ranking(seat / "knowledge.db", n=50)
    sent = [r["params"]["sql"] for r in owner.requests if r["method"] == "read"]
    assert rows and sent
    assert set(sent) <= mine, set(sent) - mine
    assert set(sent) <= registered_sql()
    # the run reached every query: live nodes, edges, contested, markers, premises, detail, evidence
    assert len(set(sent)) >= 7, sorted(set(sent))


def test_p2_t4_registered_templates_survive_registry_classification_against_a_real_schema(seat):
    """The committed entries survive `load_registry` (re-classified against the live schema,
    key == sha256(sql)); a refused template is dropped silently by the real owner, which
    would make every flag-ON run fail with refused_at_load."""
    conn = sqlite3.connect(str(seat / "knowledge.db"))
    try:
        reg = rr.load_registry(REPO_ROOT / "src" / "engram" / "engramd_read_templates.json", conn)
        mine = _sites()
        assert mine
        for sql, _site in mine:
            assert sql not in reg.refused, reg.refused.get(sql)
            assert hashlib.sha256(sql.encode("utf-8")).hexdigest() in reg.templates
    finally:
        conn.close()


# ---- differential: the same rows by either path ---------------------------------------------

def test_p2_d1_owner_and_direct_give_identical_rows(seat, monkeypatch):
    _set_flag(seat, False)
    direct = _load_tool(seat, monkeypatch).read_ranking(seat / "knowledge.db", n=50)
    _set_flag(seat, True)
    mod_on = _load_tool(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        via_owner = mod_on.read_ranking(seat / "knowledge.db", n=50)
    assert via_owner == direct
    assert len(direct) >= 6
    reasons = {r["id"]: r["weakness_reason"] for r in direct}
    assert reasons["contested"] == "contested" and reasons["stale"] == "stale"
    assert any(r["method"] == "read" for r in owner.requests), "the flag-ON leg never reached the owner"


def test_p2_d2_flag_off_never_contacts_the_owner(seat, monkeypatch):
    """Mutation (iii) -- ignore the flag, always the owner -- reddens it: no owner is up."""
    _set_flag(seat, False)
    mod = _load_tool(seat, monkeypatch)
    assert mod.read_ranking(seat / "knowledge.db", n=3)
    with FakeOwner(seat) as owner:
        assert mod.read_ranking(seat / "knowledge.db", n=3)
    assert owner.requests == []


def test_p2_d3_an_explicit_db_naming_another_store_stays_direct(seat, tmp_path, monkeypatch):
    _set_flag(seat, True)
    other = tmp_path / "other.db"
    shutil.copyfile(seat / "knowledge.db", other)
    mod = _load_tool(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        assert mod.read_ranking(other, n=3)
    assert owner.requests == []


def test_p2_d4_both_paths_leave_the_store_bytes_unchanged(seat, monkeypatch):
    _set_flag(seat, False)
    mod = _load_tool(seat, monkeypatch)
    before = (seat / "knowledge.db").read_bytes()
    assert mod.read_ranking(seat / "knowledge.db", n=50)
    assert (seat / "knowledge.db").read_bytes() == before
    _set_flag(seat, True)
    mod_on = _load_tool(seat, monkeypatch)
    with FakeOwner(seat):
        assert mod_on.read_ranking(seat / "knowledge.db", n=50)
    assert (seat / "knowledge.db").read_bytes() == before


# ---- §5.5: flag ON and the owner down / refusing / malformed RAISES -------------------------

def test_p2_c1_flag_on_owner_down_raises_never_an_empty_list_and_never_opens_the_store(seat, monkeypatch):
    """Mutation (i) -- fall back to a direct open when the owner is down -- reddens it."""
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with pytest.raises(mod.WeakestLoadBearingOwnerReadError) as exc:
        mod.read_ranking(seat / "knowledge.db")
    assert "owner" in str(exc.value).lower()
    assert NEVER_EMPTY in str(exc.value)
    assert opens.record == [], opens.record


def _owner_with(seat, answer):
    owner = FakeOwner(seat)
    owner._answer = lambda request, a=answer: (
        {"status": "ok", "result": "pong"} if request.get("method") == "ping" else a)
    return owner


@pytest.mark.parametrize("answer", [
    {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "unregistered_template", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "refused_at_load", "detail": "d", "remedy": "r"},
    {"status": "ok", "result": "not-a-list"},
])
def test_p2_c2_every_other_owner_failure_raises_too(seat, monkeypatch, answer):
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with _owner_with(seat, answer):
        with pytest.raises(mod.WeakestLoadBearingOwnerReadError) as exc:
            mod.read_ranking(seat / "knowledge.db")
    assert NEVER_EMPTY in str(exc.value), "the refusal says why it did not answer []"
    assert opens.record == []


class _BadRowsOwner(FakeOwner):
    """A real fake owner, except that ONE statement (picked by a substring of its text) is
    answered with the given rows."""

    def __init__(self, home, marker, rows):
        super().__init__(home)
        self._marker, self._rows = marker, rows

    def _answer(self, request):
        sql = (request.get("params") or {}).get("sql") or ""
        if request.get("method") == "read" and self._marker in sql:
            return {"status": "ok", "result": self._rows}
        return super()._answer(request)


# (a distinguishing substring of the statement, a malformed answer to it)
_MALFORMED = {
    "live_nodes_null_row": ("FROM nodes WHERE is_current = 1", [[None, None, None, None]]),
    "live_nodes_short": ("FROM nodes WHERE is_current = 1", [["n"]]),
    "live_nodes_bad_confidence": ("FROM nodes WHERE is_current = 1", [["n", "derivation", "high", 0]]),
    "live_nodes_not_a_row": ("FROM nodes WHERE is_current = 1", ["n"]),
    "edges_short": ("SELECT source_id, target_id FROM edges", [["a"]]),
    "edges_non_string": ("SELECT source_id, target_id FROM edges", [[1, 2]]),
    "contested_wide": ("SELECT DISTINCT e.target_id", [["a", "b"]]),
    "markers_short": ("FROM cascade_markers", [["a", "b"]]),
    "detail_missing": ("SELECT claim, quote_type, evidence_id", []),
    "detail_wide": ("SELECT claim, quote_type, evidence_id", [["c", None, None, "extra"]]),
}


@pytest.mark.parametrize("case", sorted(_MALFORMED))
def test_p2_c3_malformed_owner_rows_raise_never_a_shorter_ranking(seat, monkeypatch, case):
    """Mutation (ii) -- return [] instead of raising on a malformed answer -- reddens it."""
    marker, rows = _MALFORMED[case]
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with _BadRowsOwner(seat, marker, rows):
        with pytest.raises(mod.WeakestLoadBearingOwnerReadError) as exc:
            mod.read_ranking(seat / "knowledge.db", n=50)
    assert NEVER_EMPTY in str(exc.value)
    assert opens.record == []


def test_p2_c4_flag_on_without_the_client_module_raises_never_reads_directly(seat, monkeypatch):
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    opens = Opens().install(monkeypatch)
    with FakeOwner(seat):
        with pytest.raises(mod.WeakestLoadBearingOwnerReadError):
            mod.read_ranking(seat / "knowledge.db")
    assert opens.record == []


def test_p2_c5_the_spec_names_the_tools_own_flag_and_error_class(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    assert issubclass(mod.WeakestLoadBearingOwnerReadError, RuntimeError)
    assert mod.OWNER_FLAG_KEY == FLAG_KEY
    spec = mod._owner_spec()
    assert spec.flag_key == FLAG_KEY
    assert spec.error_cls is mod.WeakestLoadBearingOwnerReadError
    assert NEVER_EMPTY in spec.never_empty


# ---- the CLI --------------------------------------------------------------------------------

def test_p2_l1_cli_owner_down_exits_nonzero_with_no_rows_on_stdout(seat):
    """Mutation (i) reddens it: a fallback would print rows and exit 0."""
    _set_flag(seat)
    r = _cli(seat, ["--json"])
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert "WeakestLoadBearingOwnerReadError" in r.stderr and "owner" in r.stderr.lower()


def test_p2_l2_cli_malformed_owner_answer_exits_nonzero_with_no_rows(seat):
    """Mutation (ii) reddens it: [] would exit 0 and print an empty ranking."""
    _set_flag(seat)
    with _BadRowsOwner(seat, "FROM nodes WHERE is_current = 1", [[None, None, None, None]]):
        r = _cli(seat, ["--json"])
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert "WeakestLoadBearingOwnerReadError" in r.stderr


def test_p2_l3_cli_owner_up_prints_the_ranking_via_the_owner(seat):
    _set_flag(seat)
    with FakeOwner(seat) as owner:
        r = _cli(seat, ["--n", "3", "--json"])
    assert r.returncode == 0, (r.stdout, r.stderr)
    rows = json.loads(r.stdout)
    assert len(rows) == 3
    assert [x["score"] for x in rows] == sorted((x["score"] for x in rows), reverse=True)
    assert any(q["method"] == "read" for q in owner.requests)


def test_p2_l4_cli_flag_off_reads_directly_and_text_is_the_default(seat):
    _set_flag(seat, False)
    r = _cli(seat, [])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "cheapest falsifier:" in r.stdout and "turns since last recall" in r.stdout
    assert "day" not in r.stdout.lower()
    shown = [ln for ln in r.stdout.splitlines() if ln[:1].isdigit()]
    assert len(shown) == 5  # the default N


@pytest.mark.parametrize("bad_n", ["0", "-1"])
def test_p2_l6_cli_rejects_n_below_one_with_a_usage_error(seat, bad_n):
    """--n 0 / negative is a usage error naming --n (non-zero exit, nothing on stdout),
    never the 'No load-bearing beliefs found.' line, which would read as a statement
    about the graph.  Mutation: letting n through to rank reddens it."""
    _set_flag(seat, False)
    r = _cli(seat, [f"--n={bad_n}"])
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert "--n" in r.stderr
    assert "No load-bearing beliefs found" not in r.stdout + r.stderr


def test_p2_l5_cli_flag_off_matches_flag_on(seat):
    _set_flag(seat, False, turn=100)
    off = _cli(seat, ["--n", "50", "--json"])
    _set_flag(seat, True, turn=100)
    with FakeOwner(seat):
        on = _cli(seat, ["--n", "50", "--json"])
    assert off.returncode == 0 and on.returncode == 0, (off.stderr, on.stderr)
    assert json.loads(on.stdout) == json.loads(off.stdout)


# ---- turn source (Phase 2 addition) ---------------------------------------------------------

def test_p2_n1_cli_rows_carry_integer_turns_since_recall_from_the_stores_counter(seat):
    """Through the entry layer, current_turn comes from engram_core._get_current_turn():
    every row has an INTEGER turns_since_recall, never None.
    Mutation (iv) -- current_turn not passed -- reddens it (None / 'unknown')."""
    for flag in (False, True):
        _set_flag(seat, flag, turn=100)
        if flag:
            with FakeOwner(seat):
                r = _cli(seat, ["--n", "50", "--json"])
        else:
            r = _cli(seat, ["--n", "50", "--json"])
        assert r.returncode == 0, (r.stdout, r.stderr)
        rows = {x["id"]: x for x in json.loads(r.stdout)}
        assert rows
        for x in rows.values():
            assert type(x["turns_since_recall"]) is int, x
        assert rows["weak"]["turns_since_recall"] == 60  # 100 - recall_turn 40
        assert rows["contested"]["turns_since_recall"] == 90


def test_p2_n2_cli_text_prints_turns_never_unknown_when_the_store_has_a_counter(seat):
    """Mutation (iv) reddens it."""
    _set_flag(seat, False, turn=100)
    r = _cli(seat, ["--n", "50"])
    assert r.returncode == 0, r.stderr
    assert "60 turns since last recall" in r.stdout
    assert "unknown" not in r.stdout


def test_p2_n3_read_ranking_supplies_current_turn_from_the_core(seat, monkeypatch):
    """In-process: the entry layer asks engram_core._get_current_turn() (no config guess).
    Mutation (iv) reddens it."""
    _set_flag(seat, False)
    mod = _load_tool(seat, monkeypatch)
    monkeypatch.setattr(mod.core, "_get_current_turn", lambda: 1000)
    rows = {r["id"]: r for r in mod.read_ranking(seat / "knowledge.db", n=50)}
    assert rows["weak"]["turns_since_recall"] == 960
    assert type(rows["weak"]["turns_since_recall"]) is int
