"""#2243 step 3 part B -- tools/cites_step3.py, the loose-`cites` migration tool.

Contract cells, written BEFORE the implementation (contract-first). The tool
migrates ONE seat's own legacy loose `cites` edges (every `cites` that is not
observation -> evidence) to `about` or another non-provenance relation, dry-run
by default, idempotent, driven by the seat's sampling report + per-edge labels,
with the DAG audit as a hard gate around every phase.

Contract (K0-K10 of the dispatch spec):

  K0  own graph only: report.seat / report.lineage must equal the config's
      agent_name / self_lineage, else exit 2.
  K1  dry run (the default) writes nothing, creates no file without --plan-out,
      and prints the plan.
  K2  --apply preconditions (report + labels + handcheck valid, K0, reviewer
      lineage differs, edges_read >= min(20, correct-about in derivation-source
      strata) and >= 1, disagree <= 2), each refused with exit 2.
  K3  phases a-e in order, each idempotent, every retype through the engine's
      _retype_edge_impl; bulk eligibility RECOMPUTED, never read from the
      report's own decision string.
  K4  DAG audit before and after each phase; a new cycle, or a larger
      cycle_count, aborts with exit 3.
  K5  ledger / candidate / needs-seat-read files, dir 0700, files 0600.
  K6  refusals reported, never worked around; never a provenance edge.
  K7  named-edge mis-filed rate beside every no-textual-trace retype.
  K8  pending cascade marker -> deferred-pending-marker, no fallback.
  K9  a second apply changes nothing.
  K10 exit codes 0 / 2 / 3 / 1; validation before the first write.

Nodes and edges are inserted raw into a throwaway graph per cell (fixed types,
currentness and timestamps), never into any real data directory.
"""

import hashlib
import importlib.util
import itertools
import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TOOL_PATH = REPO / "tools" / "cites_step3.py"

OLD = "2026-01-01T00:00:00+00:00"
NEW = "2026-02-01T00:00:00+00:00"

_ctr = itertools.count(1)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("cites_step3_under_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class World:
    def __init__(self, core, home):
        self.core = core
        self.home = Path(home)
        self.inputs = self.home.parent / "inputs"
        self.inputs.mkdir(exist_ok=True)

    # -- graph building ---------------------------------------------------
    def conn(self):
        return self.core._get_db()

    def node(self, typ, current=1, claim=None, created=OLD):
        # evidence nodes carry the `ev_` id prefix the engine's only cites
        # writer requires of a cites target
        nid = f"{'ev' if typ == 'evidence' else 'zz'}_{next(_ctr):06d}"
        c = self.conn()
        try:
            c.execute(
                "INSERT INTO nodes (id, type, claim, created_at, is_current, confidence) "
                "VALUES (?, ?, ?, ?, ?, 0.5)",
                (nid, typ, claim if claim is not None else f"claim {nid}", created, current))
            c.commit()
        finally:
            c.close()
        return nid

    def edge(self, rel, s, t):
        c = self.conn()
        try:
            c.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                      "VALUES (?, ?, ?, ?)", (s, t, rel, self.core._now()))
            c.commit()
        finally:
            c.close()

    def cites(self, stype, ttype, src_current=1, src_claim=None, tgt_current=1):
        """A loose (or keeper) cites edge between two fresh nodes. Returns
        (source_id, target_id)."""
        t = self.node(ttype, current=tgt_current, created=OLD)
        s = self.node(stype, current=src_current, created=NEW, claim=src_claim)
        self.edge("cites", s, t)
        return s, t

    def cites_named(self, stype, ttype, src_current=1):
        """A cites edge whose source text names its target's id."""
        t = self.node(ttype, created=OLD)
        s = self.node(stype, current=src_current, created=NEW, claim=f"as discussed in {t} above")
        self.edge("cites", s, t)
        return s, t

    def eid(self, s, t, rel="cites"):
        c = self.conn()
        try:
            r = c.execute("SELECT id FROM edges WHERE source_id=? AND target_id=? AND relation=?",
                          (s, t, rel)).fetchone()
        finally:
            c.close()
        return r["id"] if r else None

    def rels(self, s, t):
        c = self.conn()
        try:
            return sorted(r["relation"] for r in c.execute(
                "SELECT relation FROM edges WHERE source_id=? AND target_id=?", (s, t)))
        finally:
            c.close()

    def edge_map(self):
        c = self.conn()
        try:
            return {r["id"]: (r["source_id"], r["target_id"], r["relation"])
                    for r in c.execute("SELECT id, source_id, target_id, relation FROM edges")}
        finally:
            c.close()

    def all_relations(self):
        return {v[2] for v in self.edge_map().values()}

    def retype_reasons(self):
        c = self.conn()
        try:
            out = []
            for r in c.execute("SELECT node_id, details FROM edit_history WHERE action='edge_retyped' ORDER BY rowid"):
                out.append((r["node_id"], json.loads(r["details"] if r["details"] else "{}")))
            return out
        finally:
            c.close()

    def marker(self, node_id):
        c = self.conn()
        try:
            c.execute(
                "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, "
                "status, created_at) VALUES (?, 'ev_x', ?, 'supersede', 'pending', ?)",
                (node_id, node_id, OLD))
            c.commit()
        finally:
            c.close()

    def resolve_markers(self):
        c = self.conn()
        try:
            c.execute("UPDATE cascade_markers SET status='resolved', resolved_at=?", (NEW,))
            c.commit()
        finally:
            c.close()

    # -- snapshots --------------------------------------------------------
    def dump(self):
        # every ordinary table, row by row (iterdump cannot read the vec0
        # virtual table without the extension)
        c = self.conn()
        try:
            h = hashlib.sha256()
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name")]
            for n in names:
                h.update(n.encode())
                for rr in sorted(repr(tuple(row)) for row in c.execute(f'SELECT * FROM "{n}"')):
                    h.update(rr.encode())
            return h.hexdigest()
        finally:
            c.close()

    def edges_hist(self):
        c = self.conn()
        try:
            e = [tuple(r) for r in c.execute("SELECT * FROM edges ORDER BY id")]
            h = [tuple(r) for r in c.execute("SELECT * FROM edit_history ORDER BY rowid")]
        finally:
            c.close()
        return e, h

    def files(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")
                      if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal")))

    # -- input files / invocation ----------------------------------------
    def write(self, name, obj):
        p = self.inputs / name
        p.write_text(json.dumps(obj))
        return str(p)


def _build_world(tmp_path, monkeypatch, dirname="home"):
    import server
    home = tmp_path / dirname
    home.mkdir()
    server._configure_paths(str(home))
    server._ensure_data_dir()
    cfg = home / "config.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data.update({"agent_name": "borges", "self_lineage": "anthropic:opus"})
    cfg.write_text(json.dumps(data))
    server.core._get_db().close()      # make sure knowledge.db exists
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    return World(server.core, home)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return _build_world(tmp_path, monkeypatch)


def stratum(src, tgt, counts, n=None, decision="BULK-ELIGIBLE"):
    n = n if n is not None else sum(counts.values())
    return {"stratum": f"{src}->{tgt}", "population": n, "n": n, "counts": counts,
            "judged": n - counts.get("cannot-judge", 0), "decision": decision}


def lab(label, rel=None, premise=None, reason="r", labeller="L1"):
    return {"label": label, "relation_if_any": rel, "premise_test": premise,
            "reason": reason, "confidence": 0.7, "labeller": labeller}


def redge(eid, s, t, st, tt, label, rel=None, named=False, premise=None, reason="r"):
    return {"edge_id": eid, "source_id": s, "target_id": t, "source_type": st,
            "target_type": tt, "target_id_named_in_source_text": named,
            "label": {"label": label, "relation_if_any": rel, "premise_test": premise,
                      "reason": reason, "confidence": 0.7}}


def report(strata=(), edges=(), seat="borges", lineage="anthropic:opus"):
    return {"seat": seat, "lineage": lineage, "frame": {"seed": 2243},
            "strata": list(strata), "edges": list(edges),
            "unchecked_axes": ["axis one", "axis two"]}


def lentry(world, s, t, labels, res=None):
    return {"edge_id": world.eid(s, t), "source_id": s, "target_id": t,
            "labels": labels, "seat_resolution": res}


def handcheck(**kw):
    h = {"reviewer": "rev", "reviewer_lineage": "openai:gpt", "seed": 2243,
         "edges_read": [1], "agree": 1, "disagree": 0}
    h.update(kw)
    return h


def _pad_report(rep, hand):
    """The hand-check must name edge ids that exist in the report's `edges`
    (G5a). Older cells pick arbitrary ids, so by default the harness adds an
    inert sampled edge (its own stratum, never in `strata`) for each integer id
    the hand-check names that the report lacks. G5a cells pass pad=False."""
    have = {e["edge_id"] for e in rep["edges"]}
    extra = [redge(i, f"pad_s{i}", f"pad_t{i}", "hc_pad", "hc_pad", "correct-about")
             for i in sorted({i for i in hand.get("edges_read", [])
                              if isinstance(i, int) and not isinstance(i, bool)})
             if i not in have]
    return dict(rep, edges=list(rep["edges"]) + extra)


def invoke(world, tool, capsys, *, rep=None, labels=None, hand=None, apply=False,
           extra=(), raw_report=None, raw_labels=None, pad=True):
    argv = []
    if hand is not None and pad and raw_report is None:
        rep = _pad_report(rep if rep is not None else report(), hand)
    rp = raw_report if raw_report is not None else (
        world.write("report.json", rep if rep is not None else report()))
    argv += ["--report", rp]
    if labels is not None or raw_labels is not None:
        argv += ["--labels", raw_labels if raw_labels is not None
                 else world.write("labels.json", labels)]
    if hand is not None:
        argv += ["--handcheck", world.write("handcheck.json", hand)]
    if apply:
        argv.append("--apply")
    argv += list(extra)
    rc = tool.main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


def apply_(world, tool, capsys, rep=None, labels=None, hand=None, extra=()):
    return invoke(world, tool, capsys, rep=rep,
                  labels=labels if labels is not None else {"edges": []},
                  hand=hand if hand is not None else handcheck(), apply=True, extra=extra)


def ledger(world):
    d = world.home / "migrations" / "cites-step3"
    files = sorted(d.glob("ledger-*.json"))
    assert files, "no ledger written"
    return json.loads(files[-1].read_text())


def entries(led, phase=None, status=None):
    es = led["entries"]
    if phase:
        es = [e for e in es if e["phase"] == phase]
    if status:
        es = [e for e in es if e["status"] == status]
    return es


def by_edge(led):
    return {(e["source_id"], e["target_id"]): e for e in led["entries"]}


# ---------------------------------------------------------------------------
# K1 -- dry run
# ---------------------------------------------------------------------------

def plant_phases(w):
    P = {}
    s, t = w.cites("task", "goal"); w.edge("derives_from", s, t); P["a_fwd"] = (s, t)
    s, t = w.cites("task", "goal"); w.edge("tensions", t, s); P["a_rev"] = (s, t)
    s, t = w.cites("task", "goal"); w.edge("tensions", s, t); w.edge("about", t, s); P["a_multi"] = (s, t)
    s, t = w.cites("task", "goal", src_current=0); w.edge("derives_from", s, t); P["a_sup"] = (s, t)
    P["b_fl"] = w.cites("feeling_report", "goal")
    P["b_q_sup"] = w.cites("question", "goal", src_current=0)
    P["c_cur"] = w.cites("definition", "goal")
    P["c_sup"] = w.cites("definition", "goal", src_current=0)
    P["c_not"] = w.cites("definition", "task")
    # the legitimate cites (observation -> ev_ evidence): it also carries a typed
    # duplicate (so phase a would take it) and its stratum is bulk-eligible in
    # the report (so phase c would take it) -- the tool must select neither
    P["keeper"] = w.cites("observation_factual", "evidence")
    w.edge("tensions", *P["keeper"])
    return P


def plant_report():
    return report(strata=[
        stratum("observation_factual", "evidence", {"correct-about": 10}),
        stratum("definition", "goal", {"correct-about": 10}),
        stratum("definition", "task", {"correct-about": 8, "mis-filed-premise": 2}, decision="BULK-ELIGIBLE"),
    ])


def test_k1_dry_run_writes_nothing_and_lists_every_planted_edge(world, tool, capsys):
    P = plant_phases(world)
    before_dump, before_files = world.dump(), world.files()
    rc, out, err = invoke(world, tool, capsys, rep=plant_report())
    assert rc == 0, out + err
    assert world.dump() == before_dump           # every table byte-identical
    assert world.files() == before_files          # no file created
    assert not (world.home / "migrations").exists()
    for key in ("a_fwd", "a_rev", "a_multi", "a_sup", "b_fl", "b_q_sup", "c_cur", "c_sup"):
        s, t = P[key]
        assert s in out and t in out, key
    for key in ("c_not", "keeper"):               # not planned
        assert P[key][0] not in out, key


def test_k1_plan_out_writes_plan_json_only_with_flag_and_only_in_dry_run(world, tool, capsys):
    P = plant_phases(world)
    dest = world.inputs / "plan.json"
    rc, out, err = invoke(world, tool, capsys, rep=plant_report(), extra=["--plan-out", str(dest)])
    assert rc == 0
    plan = json.loads(dest.read_text())
    by = by_edge(plan)
    assert by[P["a_fwd"]]["phase"] == "a" and by[P["b_fl"]]["phase"] == "b"
    assert by[P["c_cur"]]["phase"] == "c"
    assert P["c_not"] not in by and P["keeper"] not in by
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    # --plan-out is dry-run only
    rc, out, err = invoke(world, tool, capsys, rep=plant_report(), labels={"edges": []},
                          hand=handcheck(), apply=True, extra=["--plan-out", str(world.inputs / "p2.json")])
    assert rc == 2 and "plan-out" in (out + err)


def test_k1_dry_run_prints_counts_candidates_seat_reads_and_rates(world, tool, capsys):
    plant_phases(world)
    rc, out, err = invoke(world, tool, capsys, rep=plant_report(), labels={"edges": []})
    assert rc == 0
    for word in ("phase a", "phase b", "phase c", "duplicate-of:derives_from",
                 "structural", "bulk:definition->goal"):
        assert word in out, word


# ---------------------------------------------------------------------------
# K0 / K2 -- refusals (exit 2, DB untouched)
# ---------------------------------------------------------------------------

def _refused(world, tool, capsys, expect_words, **kw):
    plant_phases(world)
    snap = world.dump()
    files = world.files()
    rc, out, err = invoke(world, tool, capsys, **kw)
    assert rc == 2, out + err
    msg = out + err
    for w in expect_words:
        assert w in msg, (w, msg)
    assert world.dump() == snap
    assert world.files() == files


def test_k0_report_of_another_seat(world, tool, capsys):
    _refused(world, tool, capsys, ["otherseat", "borges"], rep=report(seat="otherseat"))


def test_k0_lineage_mismatch(world, tool, capsys):
    _refused(world, tool, capsys, ["openai:gpt", "anthropic:opus"], rep=report(lineage="openai:gpt"))


def test_k0_also_refuses_a_dry_run(world, tool, capsys):
    _refused(world, tool, capsys, ["otherseat"], rep=report(seat="otherseat"))


def test_k2_no_handcheck(world, tool, capsys):
    _refused(world, tool, capsys, ["handcheck"], rep=report(), labels={"edges": []}, apply=True)


def test_k2_same_lineage_handcheck(world, tool, capsys):
    _refused(world, tool, capsys, ["lineage"], rep=report(), labels={"edges": []},
             hand=handcheck(reviewer_lineage="anthropic:opus"), apply=True)


def test_k2_edges_read_too_short_and_boundary(world, tool, capsys):
    rep = report(strata=[stratum("derivation", "goal", {"correct-about": 30})])
    # needs min(20, 30) = 20: 19 refused
    _refused(world, tool, capsys, ["edges_read"], rep=rep, labels={"edges": []},
             hand=handcheck(edges_read=list(range(19)), agree=19), apply=True)


def test_k2_edges_read_exactly_the_minimum_passes_when_correct_about_is_large(world, tool, capsys):
    rep = report(strata=[stratum("derivation", "goal", {"correct-about": 30})])
    rc, out, err = apply_(world, tool, capsys, rep=rep,
                          hand=handcheck(edges_read=list(range(20)), agree=20))
    assert rc == 0, out + err


def test_k2_edges_read_minimum_is_the_smaller_count_when_few(world, tool, capsys):
    rep = report(strata=[stratum("derivation", "goal", {"correct-about": 3})])
    _refused(world, tool, capsys, ["edges_read"], rep=rep, labels={"edges": []},
             hand=handcheck(edges_read=[1, 2], agree=2), apply=True)
    rc, out, err = apply_(world, tool, capsys, rep=rep,
                          hand=handcheck(edges_read=[1, 2, 3], agree=3))
    assert rc == 0, out + err


def test_k2_edges_read_empty_refused_even_with_no_derivation_strata(world, tool, capsys):
    _refused(world, tool, capsys, ["edges_read"], rep=report(), labels={"edges": []},
             hand=handcheck(edges_read=[], agree=0), apply=True)


def test_k2_disagree_three_refuses_two_passes(world, tool, capsys):
    _refused(world, tool, capsys, ["disagree", "per-edge"], rep=report(), labels={"edges": []},
             hand=handcheck(disagree=3), apply=True)
    rc, out, err = apply_(world, tool, capsys, rep=report(), hand=handcheck(disagree=2))
    assert rc == 0, out + err


def test_k2_missing_labels(world, tool, capsys):
    _refused(world, tool, capsys, ["labels"], rep=report(), hand=handcheck(), apply=True)


def test_k10_malformed_report_unknown_label_value(world, tool, capsys):
    bad = report(edges=[redge(1, "a", "b", "task", "goal", "totally-new-label")])
    _refused(world, tool, capsys, ["totally-new-label", "label"], rep=bad)


def test_k10_malformed_report_missing_field_and_bad_counts(world, tool, capsys):
    bad = report()
    del bad["lineage"]
    _refused(world, tool, capsys, ["lineage"], rep=bad)
    bad2 = report(strata=[{"stratum": "task->goal", "population": 3, "n": 3,
                           "counts": {"correct-about": 3, "bogus": 1}, "judged": 3}])
    _refused(world, tool, capsys, ["bogus"], rep=bad2)


def test_k10_malformed_labels_never_partially_applies(world, tool, capsys):
    plant_phases(world)
    snap = world.dump()
    bad = {"edges": [{"edge_id": 1, "source_id": "x", "target_id": "y",
                      "labels": [lab("nonsense")], "seat_resolution": None}]}
    rc, out, err = apply_(world, tool, capsys, rep=plant_report(), labels=bad)
    assert rc == 2 and "nonsense" in (out + err)
    assert world.dump() == snap
    assert not (world.home / "migrations").exists()


def test_k10_unreadable_report_file_exit_2(world, tool, capsys):
    p = world.inputs / "nope.json"
    p.write_text("{not json")
    _refused(world, tool, capsys, ["report"], raw_report=str(p))


def test_k10_bad_phases_flag(world, tool, capsys):
    _refused(world, tool, capsys, ["phases"], rep=report(), extra=["--phases", "a,z"])


# ---------------------------------------------------------------------------
# K3 -- phases
# ---------------------------------------------------------------------------

def test_k3_a_duplicates_forward_reverse_alphabetical_and_superseded(world, tool, capsys):
    P = plant_phases(world)
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 0, out + err
    by = by_edge(ledger(world))
    assert by[P["a_fwd"]]["phase"] == "a" and by[P["a_fwd"]]["reason"] == "duplicate-of:derives_from"
    assert by[P["a_rev"]]["reason"] == "duplicate-of:tensions:reverse"
    assert by[P["a_multi"]]["reason"] == "duplicate-of:about:reverse"   # alphabetically first
    assert by[P["a_sup"]]["reason"] == "duplicate-of:derives_from"      # superseded source too
    for k in ("a_fwd", "a_rev", "a_multi", "a_sup"):
        assert by[P[k]]["status"] == "applied" and by[P[k]]["to"] == "about"
        assert "about" in world.rels(*P[k]) and "cites" not in world.rels(*P[k])
    # the typed edge itself is untouched
    assert "derives_from" in world.rels(*P["a_fwd"])


def test_k3_b_structural_feeling_report_and_question_incl_superseded(world, tool, capsys):
    P = plant_phases(world)
    apply_(world, tool, capsys, rep=plant_report())
    by = by_edge(ledger(world))
    for k in ("b_fl", "b_q_sup"):
        e = by[P[k]]
        assert (e["phase"], e["to"], e["reason"], e["status"]) == ("b", "about", "structural", "applied")
        assert world.rels(*P[k]) == ["about"]


def test_k3_c_bulk_current_and_superseded_and_keeper_untouched(world, tool, capsys):
    P = plant_phases(world)
    apply_(world, tool, capsys, rep=plant_report())
    by = by_edge(ledger(world))
    for k in ("c_cur", "c_sup"):
        e = by[P[k]]
        assert (e["phase"], e["to"], e["reason"]) == ("c", "about", "bulk:definition->goal")
        assert world.rels(*P[k]) == ["about"]
    assert world.rels(*P["c_not"]) == ["cites"]       # ineligible stratum (mis-filed)
    assert P["c_not"] not in by
    # observation -> ev_ evidence is never loose: untouched, and absent from
    # the tool's OWN ledger (this asserts the tool's selection, not an engine refusal)
    assert world.rels(*P["keeper"]) == ["cites", "tensions"]
    assert P["keeper"] not in by


def test_k3_engine_reason_string_starts_with_phase_prefix(world, tool, capsys):
    P = plant_phases(world)
    apply_(world, tool, capsys, rep=plant_report())
    reasons = [d["reason"] for _, d in world.retype_reasons()]
    assert reasons and all(r.startswith("cites-step3 ") for r in reasons)
    assert any(r.startswith("cites-step3 a:") for r in reasons)
    assert any(r.startswith("cites-step3 b:") for r in reasons)
    assert any(r.startswith("cites-step3 c:") for r in reasons)


def _eligibility_world(world):
    """Five strata, every one stamped BULK-ELIGIBLE by the report's own
    `decision`; one planted edge each."""
    def edge_of(src, tgt):
        return world.cites(src, tgt)
    S = {}
    S["ok"] = ("definition", "goal")
    S["misfiled"] = ("task", "lesson")
    S["cj25"] = ("task", "goal")
    S["j4"] = ("task", "axiom")
    S["resolves"] = ("task", "cornerstone")
    S["exempl"] = ("task", "definition")
    planted = {k: edge_of(*v) for k, v in S.items()}
    strata = [
        stratum(*S["ok"], {"correct-about": 10}),
        stratum(*S["misfiled"], {"correct-about": 9, "mis-filed-premise": 1}),
        stratum(*S["cj25"], {"correct-about": 6, "cannot-judge": 2}),
        stratum(*S["j4"], {"correct-about": 4, "cannot-judge": 1}),
        stratum(*S["resolves"], {"correct-about": 9, "other-relation": 1}),
        stratum(*S["exempl"], {"correct-about": 9, "other-relation": 1}),
    ]
    edges = [
        redge(9001, "x", "y", *S["resolves"], "other-relation", rel="resolves"),
        redge(9002, "x", "y", *S["exempl"], "other-relation", rel="exemplifies"),
    ]
    return planted, report(strata=strata, edges=edges)


def test_k3_bulk_eligibility_is_recomputed_not_trusted(world, tool, capsys):
    planted, rep = _eligibility_world(world)
    rc, out, err = apply_(world, tool, capsys, rep=rep)
    assert rc == 0, out + err
    done = {k for k, (s, t) in planted.items() if world.rels(s, t) == ["about"]}
    assert done == {"ok", "resolves"}, done
    # the ineligible ones still carry their loose cites
    for k in ("misfiled", "cj25", "j4", "exempl"):
        assert world.rels(*planted[k]) == ["cites"], k


def test_k3_bulk_eligibility_a_stratum_with_too_few_edges_taken_whole_is_not_eligible(world, tool, capsys):
    s, t = world.cites("task", "tension" if False else "goal")
    rep = report(strata=[stratum("task", "goal", {"correct-about": 3})])
    apply_(world, tool, capsys, rep=rep)
    assert world.rels(s, t) == ["cites"]


def test_k3_bulk_eligibility_reported_in_plan(world, tool, capsys):
    planted, rep = _eligibility_world(world)
    dest = world.inputs / "plan.json"
    rc, out, err = invoke(world, tool, capsys, rep=rep, extra=["--plan-out", str(dest)])
    assert rc == 0
    plan = json.loads(dest.read_text())
    el = plan["header"]["bulk_eligibility"]
    assert {k for k, v in el.items() if v["eligible"]} == {"definition->goal", "task->cornerstone"}


def test_k3_d_every_label_branch(world, tool, capsys):
    w = world
    D = {}
    labels = []

    def add(key, stype, ttype, labs, res=None, named=False):
        s, t = (w.cites_named if named else w.cites)(stype, ttype)
        D[key] = (s, t)
        labels.append(lentry(w, s, t, labs, res))

    add("correct", "lesson", "goal", [lab("correct-about")])
    add("rel_resolves", "lesson", "goal", [lab("other-relation", "resolves")])
    add("rel_unknown", "lesson", "goal", [lab("other-relation", "foo-bar")])
    add("rel_derives", "lesson", "goal", [lab("other-relation", "derives_from")])
    add("rel_serves", "task", "goal", [lab("other-relation", "serves")])
    add("rel_refused", "observation_factual", "lesson", [lab("other-relation", "instantiates")])
    add("misfiled", "derivation", "goal", [lab("mis-filed-premise", "derives_from", premise="relies",
                                          reason="the chain says 'because of X'")])
    add("cj_unnamed", "lesson", "goal", [lab("cannot-judge")])
    add("cj_named", "lesson", "goal", [lab("cannot-judge")], named=True)
    add("unforeseen", "lesson", "goal", [lab("unforeseen", reason="a new kind")])
    add("disagree", "lesson", "goal", [lab("correct-about", labeller="L1"),
                                       lab("other-relation", "serves", labeller="L2")])
    add("resolved", "lesson", "goal", [lab("correct-about", labeller="L1"),
                                       lab("other-relation", "serves", labeller="L2")],
        res={"label": "correct-about", "relation_if_any": None, "reason": "seat read it"})
    add("relies_disagree", "lesson", "goal", [lab("correct-about", premise="names-only", labeller="L1"),
                                              lab("mis-filed-premise", premise="relies",
                                                  reason="quoted sentence", labeller="L2")])
    add("agree_two", "lesson", "goal", [lab("correct-about", labeller="L1"),
                                        lab("correct-about", labeller="L2")])
    rc, out, err = apply_(w, tool, capsys, rep=report(), labels={"edges": labels})
    assert rc == 0, out + err
    led = ledger(w)
    by = by_edge(led)

    def rel_reason(k):
        e = by[D[k]]
        return e["to"], e["reason"], e["status"]

    assert rel_reason("correct") == ("about", "correct-about", "applied")
    assert rel_reason("rel_resolves") == ("about", "other-relation:resolves", "applied")
    assert rel_reason("rel_unknown") == ("about", "other-relation:foo-bar", "applied")
    assert rel_reason("rel_derives") == ("about", "other-relation:derives_from", "applied")
    assert rel_reason("rel_serves") == ("serves", "other-relation:serves", "applied")
    to, reason, status = rel_reason("rel_refused")
    assert to == "about" and status == "applied"
    assert reason.startswith("fallback:instantiates-refused:")
    assert len(reason) <= len("fallback:instantiates-refused:") + 80
    # Lei's amendment: a mis-filed premise is NEVER retyped, not even to about
    assert by[D["misfiled"]]["status"] == "needs-seat-read"
    assert by[D["misfiled"]]["to"] is None
    assert by[D["misfiled"]]["action"] == "retract+refile"
    assert rel_reason("cj_unnamed") == ("about", "no-textual-trace", "applied")
    assert by[D["cj_named"]]["status"] == "needs-seat-read"
    assert by[D["unforeseen"]]["status"] == "needs-seat-read"
    assert by[D["disagree"]]["status"] == "needs-seat-read"
    assert rel_reason("resolved") == ("about", "correct-about", "applied")
    assert by[D["relies_disagree"]]["status"] == "needs-seat-read"
    assert rel_reason("agree_two") == ("about", "correct-about", "applied")
    # the graph agrees with the ledger
    assert w.rels(*D["rel_serves"]) == ["serves"]
    assert w.rels(*D["rel_refused"]) == ["about"]
    for k in ("misfiled", "cj_named", "unforeseen", "disagree", "relies_disagree"):
        assert w.rels(*D[k]) == ["cites"], k
    # the two lists: the derivation-source mis-filed edge is on the refile list
    # (by source), the lesson-source relies-on-one-of-two edge on the promotion
    # list, each with its quoted reason, and neither is on the other's list
    cdir = w.home / "migrations" / "cites-step3"
    refile = json.loads(next(cdir.glob("refile-by-source-*.json")).read_text())
    assert [(g["source_id"], [t["target_id"] for t in g["targets"]]) for g in refile] == [
        (D["misfiled"][0], [D["misfiled"][1]])]
    assert refile[0]["action"] == "retract+refile" and refile[0]["source_type"] == "derivation"
    assert "because of X" in refile[0]["targets"][0]["reason"]
    promo = json.loads(next(cdir.glob("promotion-candidates-*.json")).read_text())
    assert [(p["source_id"], p["target_id"]) for p in promo] == [D["relies_disagree"]]
    assert "quoted sentence" in promo[0]["reason"] and promo[0]["source_type"] == "lesson"
    assert promo[0]["relies"] is True
    # needs-seat-read file: exactly the four, each with a bucket
    nsr = json.loads(next(cdir.glob("needs-seat-read-*.json")).read_text())
    assert {(n["source_id"], n["target_id"]) for n in nsr} == {
        D["misfiled"], D["cj_named"], D["unforeseen"], D["disagree"], D["relies_disagree"]}
    assert {n["bucket"] for n in nsr} == {
        "mis-filed-premise", "cannot-judge-named", "unforeseen", "labels-disagree"}
    act = {(n["source_id"], n["target_id"]): n["action"] for n in nsr}
    assert act[D["misfiled"]] == "retract+refile"
    assert [k for k, v in act.items() if v != "read"] == [D["misfiled"]]
    assert all(e["action"] == "read" for e in led["entries"]
               if e["status"] == "needs-seat-read" and (e["source_id"], e["target_id"]) != D["misfiled"])
    # fallback counted
    assert led["header"]["fallbacks"] == 1
    # no provenance edge was created, anywhere
    assert not (w.all_relations() & {"derives_from", "supported_by"})


def test_k3_d_named_flag_comes_from_report_edge_when_present(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal")      # source text does NOT name the target
    rep = report(edges=[redge(5, s, t, "lesson", "goal", "cannot-judge", named=True)])
    labels = {"edges": [lentry(w, s, t, [lab("cannot-judge")])]}
    apply_(w, tool, capsys, rep=rep, labels=labels)
    e = by_edge(ledger(w))[(s, t)]
    assert e["status"] == "needs-seat-read"
    assert w.rels(s, t) == ["cites"]


def test_k3_d_keeper_edge_in_labels_is_never_retyped(world, tool, capsys):
    s, t = world.cites("observation_factual", "evidence")
    labels = {"edges": [lentry(world, s, t, [lab("correct-about")])]}
    rc, out, err = apply_(world, tool, capsys, labels=labels)
    assert rc == 0
    assert world.rels(s, t) == ["cites"]
    assert by_edge(ledger(world))[(s, t)]["status"].startswith("refused:")


def test_k3_labels_edge_id_that_contradicts_the_graph_is_refused(world, tool, capsys):
    s, t = world.cites("lesson", "goal")
    bad = lentry(world, s, t, [lab("correct-about")])
    bad["edge_id"] = bad["edge_id"] + 1000
    snap = world.dump()
    rc, out, err = apply_(world, tool, capsys, labels={"edges": [bad]})
    assert rc == 2 and "edge_id" in (out + err)
    assert world.dump() == snap


def test_k3_e_wrong_target(world, tool, capsys):
    w = world
    s1, t1 = w.cites("lesson", "goal")
    s2, t2 = w.cites("lesson", "goal")
    labels = {"edges": [
        lentry(w, s1, t1, [lab("other-relation", "wrong-target")]),
        lentry(w, s2, t2, [lab("correct-about", reason="wrong-target")]),
    ]}
    apply_(w, tool, capsys, labels=labels)
    led = ledger(w)
    by = by_edge(led)
    for pair in ((s1, t1), (s2, t2)):
        e = by[pair]
        assert (e["phase"], e["to"], e["reason"], e["status"]) == ("e", "about", "wrong-target", "applied")
        assert w.rels(*pair) == ["about"]
    assert {(x["source_id"], x["target_id"]) for x in led["header"]["wrong_target"]} == {(s1, t1), (s2, t2)}


def test_k3_phase_order_and_phase_subset(world, tool, capsys):
    P = plant_phases(world)
    rc, out, err = apply_(world, tool, capsys, rep=plant_report(), extra=["--phases", "c,b"])
    assert rc == 0
    led = ledger(world)
    assert [e["phase"] for e in led["entries"] if e["status"] == "applied"] == sorted(
        e["phase"] for e in led["entries"] if e["status"] == "applied")
    assert {e["phase"] for e in led["entries"]} == {"b", "c"}
    assert world.rels(*P["a_fwd"]) == sorted(["cites", "derives_from"])


def test_k3_phase_a_edge_also_in_labels_is_skipped_not_cites_in_d(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal")
    w.edge("tensions", s, t)
    labels = {"edges": [lentry(w, s, t, [lab("correct-about")])]}
    apply_(w, tool, capsys, labels=labels)
    es = [e for e in ledger(w)["entries"] if (e["source_id"], e["target_id"]) == (s, t)]
    assert [(e["phase"], e["status"]) for e in es] == [("a", "applied"), ("d", "skipped-not-cites")]


def test_k3_engine_path_spy_and_source_has_no_raw_writes(world, tool, capsys, monkeypatch):
    import engram_cornerstone
    P = plant_phases(world)
    calls = []
    real = engram_cornerstone._retype_edge_impl

    def spy(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(engram_cornerstone, "_retype_edge_impl", spy)
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 0
    applied = [e for e in ledger(world)["entries"] if e["status"] == "applied"]
    assert applied and len(calls) == len(applied)
    assert all(c["relation"] == "cites" and c["reason"].startswith("cites-step3 ") for c in calls)
    assert {(c["source_id"], c["target_id"]) for c in calls} == {
        (e["source_id"], e["target_id"]) for e in applied}


def test_k3_tool_source_never_issues_its_own_writes():
    src = TOOL_PATH.read_text()
    for pat in (r"update\s+edges", r"insert\s+into\s+edges", r"delete\s+from\s+edges",
                r"executescript"):
        assert not re.search(pat, src, re.IGNORECASE), pat


# ---------------------------------------------------------------------------
# K4 -- DAG audit gate
# ---------------------------------------------------------------------------

def _fake_audit(monkeypatch, script):
    """script: list of (cycles, cycle_count) returned on successive calls (the
    last repeats). Records calls."""
    import engram_dag_conformance as dagc
    calls = []

    def fake(conn):
        i = min(len(calls), len(script) - 1)
        calls.append(i)
        cycles, count = script[i]
        return {"cycles": cycles, "cycle_count": count, "cycle_found": bool(cycles),
                "cycle_path": cycles[0]["path"] if cycles else [], "cycles_truncated": False,
                "families": {}, "dag_relation_count": 0}

    monkeypatch.setattr(dagc, "check_dag_conformance", fake)
    return calls


CYC = {"family": "grounding", "path": ["zz_a", "zz_b", "zz_a"]}
CYC_ROT = {"family": "grounding", "path": ["zz_b", "zz_a", "zz_b"]}      # same cycle, rotated
CYC2 = {"family": "grounding", "path": ["zz_c", "zz_d", "zz_c"]}


def test_k4_new_cycle_after_phase_b_aborts_before_c(world, tool, capsys, monkeypatch):
    P = plant_phases(world)
    # audit calls: baseline, after a, after b (<- new cycle), ...
    _fake_audit(monkeypatch, [([], 0), ([], 0), ([CYC2], 1)])
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 3, out + err
    assert "zz_c" in out + err and "aborted-audit" in out + err
    led = ledger(world)
    assert led["header"]["status"] == "aborted-audit"
    assert {e["phase"] for e in led["entries"]} == {"a", "b"}       # (c)-(e) never ran
    assert world.rels(*P["c_cur"]) == ["cites"]                        # untouched
    assert world.rels(*P["b_fl"]) == ["about"]                         # committed, NOT rolled back
    assert "not rolled back" in (out + err).lower() or "no inverse" in (out + err).lower()


def test_k4_unchanged_cycle_set_but_larger_cycle_count_aborts(world, tool, capsys, monkeypatch):
    plant_phases(world)
    _fake_audit(monkeypatch, [([CYC], 1), ([CYC], 1), ([CYC_ROT], 2)])
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 3, out + err
    assert ledger(world)["header"]["status"] == "aborted-audit"


def test_k4_preexisting_unchanged_cycle_does_not_abort_and_rotation_is_canonicalised(
        world, tool, capsys, monkeypatch):
    plant_phases(world)
    # the SAME cycle reported rotated after a phase is not new
    _fake_audit(monkeypatch, [([CYC], 1), ([CYC_ROT], 1)])
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 0, out + err
    assert ledger(world)["header"].get("status") != "aborted-audit"


def test_k4_audit_runs_before_first_phase_and_after_each(world, tool, capsys, monkeypatch):
    plant_phases(world)
    calls = _fake_audit(monkeypatch, [([], 0)])
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 0
    assert len(calls) == 1 + 5          # baseline + one per phase a..e
    led = ledger(world)
    assert led["header"]["audit"]["before"]["cycle_count"] == 0
    assert len(led["header"]["audit"]["after_phase"]) == 5


def test_k4_real_audit_clean_graph_passes(world, tool, capsys):
    plant_phases(world)
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 0
    assert ledger(world)["header"]["audit"]["final"]["cycle_count"] == 0


# ---------------------------------------------------------------------------
# K5 -- ledger and outputs
# ---------------------------------------------------------------------------

def test_k5_ledger_equals_db_changes_both_ways_and_modes(world, tool, capsys):
    P = plant_phases(world)
    before = world.edge_map()
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 0
    after = world.edge_map()
    changed = {i: after[i][2] for i in after if after[i][2] != before.get(i, (0, 0, None))[2]}
    led = ledger(world)
    applied = {e["edge_id"]: e["to"] for e in led["entries"] if e["status"] == "applied"}
    assert applied == changed and changed
    for e in led["entries"]:
        assert set(e) >= {"phase", "edge_id", "source_id", "target_id", "from", "to", "reason", "status"}
        assert e["from"] == "cites"
    d = world.home / "migrations" / "cites-step3"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    names = [p.name for p in d.iterdir()]
    assert any(n.startswith("ledger-") for n in names)
    assert any(n.startswith("refile-by-source-") for n in names)
    assert any(n.startswith("promotion-candidates-") for n in names)
    assert any(n.startswith("needs-seat-read-") for n in names)
    for p in d.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name


def test_k5_summary_counts_and_unchecked_axes_copied(world, tool, capsys):
    plant_phases(world)
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    h = ledger(world)["header"]
    assert h["unchecked_axes"] == ["axis one", "axis two"]
    assert h["counts_per_phase"]["a"]["applied"] == 4
    assert h["counts_per_phase"]["b"]["applied"] == 2
    assert h["counts_per_phase"]["c"]["applied"] == 2
    assert h["counts_per_reason"]["structural"] == 2
    assert h["counts_per_status"]["applied"] == 8
    assert "axis one" in out


# ---------------------------------------------------------------------------
# K6 / K7 / K8 / K9
# ---------------------------------------------------------------------------

def test_k6_provenance_relation_named_in_label_yields_about_never_provenance(world, tool, capsys):
    w = world
    pairs = [w.cites("lesson", "goal") for _ in range(2)]
    labels = {"edges": [
        lentry(w, *pairs[0], [lab("other-relation", "supported_by")]),
        lentry(w, *pairs[1], [lab("other-relation", "derives_from")]),
    ]}
    apply_(w, tool, capsys, labels=labels)
    for p in pairs:
        assert w.rels(*p) == ["about"]
    assert not (w.all_relations() & {"derives_from", "supported_by", "cites"})


def test_k7_named_edge_mis_filed_rate_beside_no_textual_trace(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal")           # unnamed, cannot-judge -> no-textual-trace
    labels = {"edges": [lentry(w, s, t, [lab("cannot-judge")])]}
    sampled = [
        redge(1, "a", "b", "lesson", "goal", "correct-about", named=True),
        redge(2, "a", "b", "lesson", "goal", "correct-about", named=True),
        redge(3, "a", "b", "lesson", "goal", "mis-filed-premise", named=True),
        redge(4, "a", "b", "lesson", "goal", "cannot-judge", named=False),
        redge(5, "a", "b", "task", "goal", "mis-filed-premise", named=True),   # other stratum
    ]
    rc, out, err = apply_(w, tool, capsys, rep=report(edges=sampled), labels=labels)
    assert rc == 0
    k7 = ledger(w)["header"]["named_edge_mis_filed_rate"]
    assert k7 == {"lesson->goal": {"named_sampled": 3, "named_mis_filed": 1}}
    assert "lesson->goal" in out and "named_mis_filed" in out


def test_k8_pending_marker_defers_then_second_run_applies(world, tool, capsys):
    w = world
    s, t = w.cites("feeling_report", "goal")
    w.marker(t)
    rc, out, err = apply_(w, tool, capsys, rep=report())
    assert rc == 0, out + err
    e = by_edge(ledger(w))[(s, t)]
    assert e["status"] == "deferred-pending-marker" and w.rels(s, t) == ["cites"]
    w.resolve_markers()
    rc, out, err = apply_(w, tool, capsys, rep=report())
    assert rc == 0
    assert w.rels(s, t) == ["about"]
    assert any(x["status"] == "applied" for x in ledger(w)["entries"])


def test_k8_marker_on_addable_relation_is_deferred_not_fallback(world, tool, capsys):
    w = world
    s, t = w.cites("task", "goal")
    w.marker(s)
    labels = {"edges": [lentry(w, s, t, [lab("other-relation", "serves")])]}
    apply_(w, tool, capsys, labels=labels)
    led = ledger(w)
    assert by_edge(led)[(s, t)]["status"] == "deferred-pending-marker"
    assert led["header"]["fallbacks"] == 0
    assert w.rels(s, t) == ["cites"]


def test_k9_second_apply_changes_nothing_and_applies_zero(world, tool, capsys):
    P = plant_phases(world)
    labels_edges = []
    s, t = world.cites("lesson", "goal")
    labels_edges.append(lentry(world, s, t, [lab("correct-about")]))
    s2, t2 = world.cites("lesson", "goal")
    labels_edges.append(lentry(world, s2, t2, [lab("unforeseen")]))
    labels = {"edges": labels_edges}
    rc, out, err = apply_(world, tool, capsys, rep=plant_report(), labels=labels)
    assert rc == 0
    eh1 = world.edges_hist()
    rc, out, err = apply_(world, tool, capsys, rep=plant_report(), labels=labels)
    assert rc == 0, out + err
    assert world.edges_hist() == eh1
    led = ledger(world)
    assert not entries(led, status="applied")
    # the labelled edge that was already retyped is SKIPPED, not refused by the engine
    assert by_edge(led)[(s, t)]["status"] == "skipped-not-cites"
    assert by_edge(led)[(s2, t2)]["status"] == "needs-seat-read"
    assert not [e for e in led["entries"] if e["status"].startswith("refused")]


# ---------------------------------------------------------------------------
# Lei's amendment: mis-filed-premise is never retyped (retract+refile)
# ---------------------------------------------------------------------------

def _misfiled_world(w):
    s, t = w.cites("derivation", "goal")
    labels = {"edges": [lentry(w, s, t, [lab("mis-filed-premise", "derives_from", premise="relies",
                                              reason="quoted: 'because of X'")])]}
    return s, t, labels


def test_amend_misfiled_left_as_cites_in_dry_run_plan(world, tool, capsys):
    s, t, labels = _misfiled_world(world)
    snap = world.dump()
    dest = world.inputs / "plan.json"
    rc, out, err = invoke(world, tool, capsys, rep=report(), labels=labels, extra=["--plan-out", str(dest)])
    assert rc == 0
    assert world.dump() == snap
    plan = json.loads(dest.read_text())
    e = by_edge(plan)[(s, t)]
    assert e["status"] == "needs-seat-read" and e["to"] is None and e["action"] == "retract+refile"
    assert "retract+refile" in out
    assert [(g["source_id"], [x["target_id"] for x in g["targets"]])
            for g in plan["refile_by_source"]] == [(s, [t])]
    assert plan["promotion_candidates"] == []


def test_amend_misfiled_left_as_cites_in_apply_ledger_and_file(world, tool, capsys):
    s, t, labels = _misfiled_world(world)
    rc, out, err = apply_(world, tool, capsys, labels=labels)
    assert rc == 0
    assert world.rels(s, t) == ["cites"]
    assert world.retype_reasons() == []
    e = by_edge(ledger(world))[(s, t)]
    assert e["status"] == "needs-seat-read" and e["action"] == "retract+refile"
    d = world.home / "migrations" / "cites-step3"
    nsr = json.loads(next(d.glob("needs-seat-read-*.json")).read_text())
    assert [(n["source_id"], n["target_id"], n["action"]) for n in nsr] == [(s, t, "retract+refile")]


def test_amend_misfiled_with_seat_resolution_is_also_not_retyped(world, tool, capsys):
    w = world
    s, t = w.cites("derivation", "goal")
    labels = {"edges": [lentry(w, s, t, [lab("correct-about"), lab("mis-filed-premise", labeller="L2")],
                               res={"label": "mis-filed-premise", "relation_if_any": "derives_from",
                                    "reason": "seat says premise"})]}
    apply_(w, tool, capsys, labels=labels)
    assert w.rels(s, t) == ["cites"]
    assert by_edge(ledger(w))[(s, t)]["action"] == "retract+refile"


# ---------------------------------------------------------------------------
# Kepler's folds: refile list grouped by source; non-derivation sources are
# retyped to about and exported on the promotion list instead
# ---------------------------------------------------------------------------

def _mf(w, source, ttype="goal"):
    """A mis-filed-premise edge from an EXISTING source node to a fresh target."""
    t = w.node(ttype, created=OLD)
    w.edge("cites", source, t)
    return t


def test_fold1_two_targets_of_one_derivation_source_make_one_entry(world, tool, capsys):
    w = world
    src = w.node("derivation", created=NEW)
    t1, t2 = _mf(w, src), _mf(w, src)
    other = w.node("derivation", created=NEW)
    t3 = _mf(w, other)
    labels = {"edges": [lentry(w, src, t, [lab("mis-filed-premise", "derives_from", premise="relies",
                                                reason=f"quote for {i}")])
                        for i, t in enumerate((t1, t2))]
              + [lentry(w, other, t3, [lab("mis-filed-premise", "derives_from", reason="other quote")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    d = w.home / "migrations" / "cites-step3"
    refile = json.loads(next(d.glob("refile-by-source-*.json")).read_text())
    by_src = {g["source_id"]: g for g in refile}
    assert len(refile) == 2 and set(by_src) == {src, other}
    assert sorted(t["target_id"] for t in by_src[src]["targets"]) == sorted([t1, t2])
    assert all(t["reason"] and t["edge_id"] for t in by_src[src]["targets"])
    assert by_src[src]["action"] == "retract+refile" and by_src[src]["source_type"] == "derivation"
    # counts reported per edge AND per source
    h = ledger(w)["header"]
    assert h["refile_counts"] == {"edges": 3, "sources": 2}
    # still loose cites, none retyped
    for t in (t1, t2, t3):
        assert "cites" in w.rels(src if t in (t1, t2) else other, t)


def test_fold2_non_derivation_source_is_retyped_and_promoted_not_refiled(world, tool, capsys):
    w = world
    s_les, t_les = w.cites("lesson", "goal")
    s_obs, t_obs = w.cites("observation_factual", "goal")
    s_der, t_der = w.cites("derivation", "goal")
    mf = lambda reason: [lab("mis-filed-premise", "derives_from", premise="relies", reason=reason)]
    labels = {"edges": [lentry(w, s_les, t_les, mf("lesson quote")),
                        lentry(w, s_obs, t_obs, mf("obs quote")),
                        lentry(w, s_der, t_der, mf("derivation quote"))]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    by = by_edge(ledger(w))
    for p in ((s_les, t_les), (s_obs, t_obs)):
        e = by[p]
        assert (e["to"], e["reason"], e["status"]) == ("about", "non-derivation-premise", "applied")
        assert w.rels(*p) == ["about"]
    assert w.rels(s_der, t_der) == ["cites"]
    assert by[(s_der, t_der)]["status"] == "needs-seat-read"
    d = w.home / "migrations" / "cites-step3"
    refile = json.loads(next(d.glob("refile-by-source-*.json")).read_text())
    promo = json.loads(next(d.glob("promotion-candidates-*.json")).read_text())
    assert [g["source_id"] for g in refile] == [s_der]
    assert {(p["source_id"], p["target_id"]): p["source_type"] for p in promo} == {
        (s_les, t_les): "lesson", (s_obs, t_obs): "observation_factual"}
    assert all(p["reason"] and p["edge_id"] and "relies" in p for p in promo)
    assert ledger(w)["header"]["promotion_candidate_count"] == 2
    # the engine reason string for the retype names the phase and the code
    reasons = [d_["reason"] for _, d_ in w.retype_reasons()]
    assert "cites-step3 d: non-derivation-premise" in reasons


# ---------------------------------------------------------------------------
# Fold 3: a refile candidate whose target is already a transitive premise
# ancestor of its source is "already covered" -- not refiled, cites -> about
# ---------------------------------------------------------------------------

def _mf_labels(w, pairs):
    return {"edges": [lentry(w, s, t, [lab("mis-filed-premise", "derives_from", premise="relies",
                                            reason=f"quote {i}")])
                      for i, (s, t) in enumerate(pairs)]}


def _src_tgt(w):
    """A derivation source with a loose cites to a fresh target."""
    t = w.node("derivation", created=OLD)
    s = w.node("derivation", created=NEW)
    w.edge("cites", s, t)
    return s, t


def _read(w, prefix):
    d = w.home / "migrations" / "cites-step3"
    return json.loads(next(d.glob(f"{prefix}-*.json")).read_text())


def test_fold3_a_two_hop_chain_is_covered_retyped_and_off_the_refile_list(world, tool, capsys):
    w = world
    s, t = _src_tgt(w)
    m = w.node("derivation", created=OLD)
    w.edge("derives_from", s, m)
    w.edge("supported_by", m, t)
    rc, out, err = apply_(w, tool, capsys, labels=_mf_labels(w, [(s, t)]))
    assert rc == 0, out + err
    e = by_edge(ledger(w))[(s, t)]
    assert (e["to"], e["reason"], e["status"]) == ("about", "already-covered", "applied")
    assert w.rels(s, t) == ["about"]
    assert _read(w, "refile-by-source") == []
    cov = _read(w, "already-covered")
    assert len(cov) == 1
    assert (cov[0]["source_id"], cov[0]["target_id"], cov[0]["path"]) == (s, t, [s, m, t])
    assert cov[0]["edge_id"] and cov[0]["reason"] == "quote 0"
    h = ledger(w)["header"]
    assert h["already_covered_counts"] == {"edges": 1, "sources": 1}
    assert h["refile_counts"] == {"edges": 0, "sources": 0}
    # the premise edges themselves are untouched
    assert w.rels(s, m) == ["derives_from"] and w.rels(m, t) == ["supported_by"]
    # the engine's reason string
    assert "cites-step3 d: already-covered" in [d_["reason"] for _, d_ in w.retype_reasons()]


def test_fold3_a_direct_parent_control_and_dry_run_listing(world, tool, capsys):
    w = world
    s, t = _src_tgt(w)
    w.edge("derives_from", s, t)           # direct premise
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, rep=report(), labels=_mf_labels(w, [(s, t)]),
                          extra=["--phases", "d", "--plan-out", str(dest)])
    assert rc == 0
    plan = json.loads(dest.read_text())
    assert [(c["source_id"], c["target_id"], c["path"]) for c in plan["already_covered"]] == [(s, t, [s, t])]
    assert plan["refile_by_source"] == []
    assert by_edge(plan)[(s, t)]["reason"] == "already-covered"
    assert w.rels(s, t) == ["cites", "derives_from"]      # dry run: untouched


def test_fold3_b_no_path_stays_on_refile_list_cites_untouched(world, tool, capsys):
    w = world
    s, t = _src_tgt(w)
    m = w.node("derivation", created=OLD)
    w.edge("derives_from", s, m)           # a premise chain, but not to t
    apply_(w, tool, capsys, labels=_mf_labels(w, [(s, t)]))
    assert w.rels(s, t) == ["cites"]
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [s]
    assert _read(w, "already-covered") == []


def test_fold3_c_path_through_superseded_intermediate_does_not_count(world, tool, capsys):
    w = world
    s, t = _src_tgt(w)
    m = w.node("derivation", current=0, created=OLD)
    w.edge("derives_from", s, m)
    w.edge("supported_by", m, t)
    apply_(w, tool, capsys, labels=_mf_labels(w, [(s, t)]))
    assert w.rels(s, t) == ["cites"]
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [s]
    # control: a second, CURRENT route does count
    w2 = world
    s2, t2 = _src_tgt(w2)
    dead = w2.node("derivation", current=0, created=OLD)
    live = w2.node("derivation", created=OLD)
    w2.edge("derives_from", s2, dead); w2.edge("supported_by", dead, t2)
    w2.edge("derives_from", s2, live); w2.edge("supported_by", live, t2)
    apply_(w2, tool, capsys, labels=_mf_labels(w2, [(s2, t2)]))
    assert w2.rels(s2, t2) == ["about"]


def test_fold3_d_cyclic_premise_graph_terminates(world, tool, capsys):
    w = world
    s, t = _src_tgt(w)
    a = w.node("derivation", created=OLD)
    b = w.node("derivation", created=OLD)
    w.edge("derives_from", s, a); w.edge("derives_from", a, b); w.edge("derives_from", b, a)
    rc, out, err = apply_(w, tool, capsys, labels=_mf_labels(w, [(s, t)]))
    assert rc == 0, out + err
    assert w.rels(s, t) == ["cites"]
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [s]


def test_fold3_e_hop_cap_is_a_named_bound(world, tool, capsys, monkeypatch):
    assert tool.MAX_ANCESTRY_HOPS == 50
    monkeypatch.setattr(tool, "MAX_ANCESTRY_HOPS", 3)
    w = world

    def chain(hops):
        s, t = _src_tgt(w)
        prev = s
        for _ in range(hops - 1):
            n = w.node("derivation", created=OLD)
            w.edge("derives_from", prev, n)
            prev = n
        w.edge("derives_from", prev, t)
        return s, t

    ok, far = chain(3), chain(4)
    apply_(w, tool, capsys, labels=_mf_labels(w, [ok, far]))
    assert w.rels(*ok) == ["about"]             # exactly at the cap: covered
    assert w.rels(*far) == ["cites"]            # past the cap: NOT covered
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [far[0]]


def test_fold3_f_an_about_only_path_is_not_ancestry(world, tool, capsys):
    w = world
    s, t = _src_tgt(w)
    m = w.node("derivation", created=OLD)
    w.edge("about", s, m)
    w.edge("derives_from", m, t)
    apply_(w, tool, capsys, labels=_mf_labels(w, [(s, t)]))
    assert w.rels(s, t) == ["cites"]
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [s]


def test_fold3_g_non_derivation_source_never_goes_through_the_filter(world, tool, capsys):
    w = world
    t = w.node("derivation", created=OLD)
    s = w.node("lesson", created=NEW)
    w.edge("cites", s, t)
    w.edge("derives_from", s, t)           # a path exists, but the source is not a derivation
    apply_(w, tool, capsys, labels=_mf_labels(w, [(s, t)]), extra=["--phases", "d"])
    e = by_edge(ledger(w))[(s, t)]
    assert e["reason"] == "non-derivation-premise"
    assert _read(w, "already-covered") == []


# ---------------------------------------------------------------------------
# One definition of "the keeper pair": the tool asks the engine's shared
# predicate (core._cites_pair_error), it does not re-implement the test
# ---------------------------------------------------------------------------

def test_keeper_selection_uses_the_shared_engine_predicate_in_every_phase(
        world, tool, capsys, monkeypatch):
    w = world
    plant_phases(w)
    s_d, t_d = w.cites("lesson", "goal")           # phase d target
    s_e, t_e = w.cites("lesson", "goal")           # phase e target
    labels = {"edges": [lentry(w, s_d, t_d, [lab("correct-about")]),
                        lentry(w, s_e, t_e, [lab("other-relation", "wrong-target")])]}
    # every edge above is loose by the ordinary rule; the patched shared
    # predicate calls every pair legitimate, so the tool must select none
    seen = []

    def accept_everything(conn, source_id, target_id):
        seen.append((source_id, target_id))
        return None

    monkeypatch.setattr(w.core, "_cites_pair_error", accept_everything)
    before = w.edge_map()
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, rep=plant_report(), labels=labels,
                          extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    def only_keeper_refusals(es):
        # a/b/c select from the graph and see nothing; d/e take the labelled
        # edges and record each as "keeper, refused" -- never planned/applied
        assert {e["phase"] for e in es} == {"d", "e"}
        assert {(e["reason"], e["status"]) for e in es} == {("keeper", "refused:not-loose-cites")}
        assert {(e["source_id"], e["target_id"]) for e in es} == {(s_d, t_d), (s_e, t_e)}

    only_keeper_refusals(json.loads(dest.read_text())["entries"])   # dry run
    assert seen                                                    # the shared predicate was consulted
    rc, out, err = apply_(w, tool, capsys, rep=plant_report(), labels=labels)
    assert rc == 0, out + err
    only_keeper_refusals(ledger(w)["entries"])
    assert w.edge_map() == before
    assert w.retype_reasons() == []


# ---------------------------------------------------------------------------
# lost-relation export (Kepler's ruling): a sampled edge whose effective label
# names a NON-addable relation is listed per stratum, so the seat can re-home
# the real link by hand (the tool never calls the proper verb itself)
# ---------------------------------------------------------------------------

def _lost(w):
    return _read(w, "lost-relation")


def _lost_pairs(doc):
    return {(e["source_id"], e["target_id"]): e
            for es in doc["strata"].values() for e in es}


def _bulk_world_with_resolves(w):
    """A bulk-eligible definition->goal stratum, one REAL sampled edge in it
    labelled `resolves`."""
    pairs = [w.cites("definition", "goal") for _ in range(6)]
    s, t = pairs[0]
    eid = w.eid(s, t)
    rep = report(
        strata=[stratum("definition", "goal", {"correct-about": 9, "other-relation": 1})],
        edges=[redge(eid, s, t, "definition", "goal", "other-relation", rel="resolves",
                     reason="the claim answers this question")])
    return pairs, (s, t), eid, rep


def test_lost_a_bulk_stratum_resolves_edge_is_listed_and_still_retyped_to_about(world, tool, capsys):
    w = world
    pairs, (s, t), eid, rep = _bulk_world_with_resolves(w)
    rc, out, err = apply_(w, tool, capsys, rep=rep)
    assert rc == 0, out + err
    by = by_edge(ledger(w))
    assert (by[(s, t)]["to"], by[(s, t)]["reason"]) == ("about", "bulk:definition->goal")
    assert w.rels(s, t) == ["about"]
    doc = _lost(w)
    assert doc["header"] == {"total": 1, "per_stratum": {"definition->goal": 1}}
    assert list(doc["strata"]) == ["definition->goal"]
    (entry,) = doc["strata"]["definition->goal"]
    assert entry == {"edge_id": eid, "source_id": s, "target_id": t,
                     "relation_named": "resolves",
                     "reason": "the claim answers this question",
                     "phase_applied": "c", "action_taken": "about"}
    f = next((w.home / "migrations" / "cites-step3").glob("lost-relation-*.json"))
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_lost_b_per_edge_other_relation_supersedes_and_unknown_name_are_listed(world, tool, capsys):
    w = world
    s1, t1 = w.cites("lesson", "goal")
    s2, t2 = w.cites("lesson", "goal")
    labels = {"edges": [lentry(w, s1, t1, [lab("other-relation", "supersedes", reason="replaces the old one")]),
                        lentry(w, s2, t2, [lab("other-relation", "foo-bar", reason="odd name")])]}
    eid1 = w.eid(s1, t1)
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    doc = _lost(w)
    got = _lost_pairs(doc)
    assert set(got) == {(s1, t1), (s2, t2)}
    e1, e2 = got[(s1, t1)], got[(s2, t2)]
    assert (e1["relation_named"], e1["phase_applied"], e1["action_taken"]) == ("supersedes", "d", "about")
    assert e1["reason"] == "replaces the old one" and e1["edge_id"] == eid1
    assert (e2["relation_named"], e2["phase_applied"], e2["action_taken"]) == ("foo-bar", "d", "about")
    assert doc["header"] == {"total": 2, "per_stratum": {"lesson->goal": 2}}
    assert w.rels(s1, t1) == ["about"] and w.rels(s2, t2) == ["about"]


def test_lost_c_an_addable_relation_is_not_listed(world, tool, capsys):
    w = world
    s, t = w.cites("task", "goal")
    s2, t2 = w.cites("lesson", "goal")
    labels = {"edges": [lentry(w, s, t, [lab("other-relation", "serves")]),
                        lentry(w, s2, t2, [lab("correct-about")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    assert w.rels(s, t) == ["serves"]
    doc = _lost(w)
    assert doc["header"] == {"total": 0, "per_stratum": {}} and doc["strata"] == {}


def test_lost_d_keeper_pair_with_a_non_addable_label_is_not_listed_and_not_acted_on(world, tool, capsys):
    w = world
    s, t = w.cites("observation_factual", "evidence")
    labels = {"edges": [lentry(w, s, t, [lab("other-relation", "supersedes")])]}
    rep = report(strata=[], edges=[redge(w.eid(s, t), s, t, "observation_factual", "evidence",
                                         "other-relation", rel="resolves")])
    rc, out, err = apply_(w, tool, capsys, rep=rep, labels=labels)
    assert rc == 0, out + err
    assert w.rels(s, t) == ["cites"]
    assert by_edge(ledger(w))[(s, t)]["status"].startswith("refused:")
    doc = _lost(w)
    assert doc["header"] == {"total": 0, "per_stratum": {}} and doc["strata"] == {}


def test_lost_e_dry_run_lists_the_entries_and_writes_nothing_to_the_graph(world, tool, capsys):
    w = world
    pairs, (s, t), eid, rep = _bulk_world_with_resolves(w)
    s2, t2 = w.cites("lesson", "goal")
    labels = {"edges": [lentry(w, s2, t2, [lab("other-relation", "contradicts", reason="opposes it")])]}
    snap, files = w.dump(), w.files()
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, rep=rep, labels=labels, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    assert w.dump() == snap and w.files() == files and not (w.home / "migrations").exists()
    plan = json.loads(dest.read_text())
    got = _lost_pairs(plan["lost_relation"])
    assert set(got) == {(s, t), (s2, t2)}
    assert (got[(s, t)]["phase_applied"], got[(s, t)]["action_taken"]) == ("c", "about")
    assert (got[(s2, t2)]["phase_applied"], got[(s2, t2)]["action_taken"]) == ("d", "about")
    assert plan["lost_relation"]["header"]["total"] == 2
    assert "lost-relation" in out and "resolves" in out and "contradicts" in out


def test_lost_edges_not_acted_on_are_listed_with_null_phase_and_none_action(world, tool, capsys):
    w = world
    # a sampled edge that is not a graph edge at all, and one whose phase is run-limited away
    s, t = w.cites("lesson", "goal")
    rep = report(edges=[redge(77, "gone_src", "gone_tgt", "lesson", "goal", "other-relation",
                              rel="resolves", reason="not in this graph")])
    labels = {"edges": [lentry(w, s, t, [lab("other-relation", "supersedes", reason="r")])]}
    rc, out, err = apply_(w, tool, capsys, rep=rep, labels=labels, extra=["--phases", "a"])
    assert rc == 0, out + err
    got = _lost_pairs(_lost(w))
    assert set(got) == {("gone_src", "gone_tgt"), (s, t)}
    for e in got.values():
        assert (e["phase_applied"], e["action_taken"]) == (None, "none")
    assert w.rels(s, t) == ["cites"]


def test_lost_labels_file_effective_label_wins_and_unresolved_or_wrong_target_are_not_listed(
        world, tool, capsys):
    w = world
    s1, t1 = w.cites("lesson", "goal")      # report says resolves, labels file says correct-about
    s2, t2 = w.cites("lesson", "goal")      # two labels disagree: no effective label
    s3, t3 = w.cites("lesson", "goal")      # wrong-target sentinel is a flag, not a relation
    s4, t4 = w.cites("lesson", "goal")      # seat_resolution names a lost relation
    rep = report(edges=[redge(w.eid(s1, t1), s1, t1, "lesson", "goal", "other-relation", rel="resolves")])
    labels = {"edges": [
        lentry(w, s1, t1, [lab("correct-about")]),
        lentry(w, s2, t2, [lab("other-relation", "supersedes", labeller="L1"),
                           lab("correct-about", labeller="L2")]),
        lentry(w, s3, t3, [lab("other-relation", "wrong-target")]),
        lentry(w, s4, t4, [lab("correct-about", labeller="L1"), lab("correct-about", labeller="L2")],
               res={"label": "other-relation", "relation_if_any": "resolves", "reason": "seat read"}),
    ]}
    rc, out, err = apply_(w, tool, capsys, rep=rep, labels=labels)
    assert rc == 0, out + err
    assert set(_lost_pairs(_lost(w))) == {(s4, t4)}


# ===========================================================================
# Review round 1 folds (F1-F9)
# ===========================================================================

HAND10 = handcheck(edges_read=list(range(10)), agree=10)


def _mf_label(reason="quoted: 'because of X'", rel="derives_from", premise="relies", labeller="L1"):
    return lab("mis-filed-premise", rel, premise=premise, reason=reason, labeller=labeller)


def _entries_of(led, pair):
    return [(e["phase"], e["status"]) for e in led["entries"]
            if (e["source_id"], e["target_id"]) == pair]


# ---------------------------------------------------------------------------
# F1 -- a labelled mis-filed derivation edge is never retyped in ANY phase,
# bulk included; phase c leaves every labelled edge to the per-edge pass
# ---------------------------------------------------------------------------

def _bulk_deriv_world(w):
    """A derivation->goal stratum that is bulk-eligible by its counts. Planted:
    A labelled mis-filed (retract+refile), B labelled correct-about, C unlabelled."""
    A, B, C = (w.cites("derivation", "goal") for _ in range(3))
    rep = report(strata=[stratum("derivation", "goal", {"correct-about": 10})])
    labels = {"edges": [lentry(w, *A, [_mf_label()]),
                        lentry(w, *B, [lab("correct-about")])]}
    return A, B, C, rep, labels


def test_f1_bulk_phase_skips_a_labelled_misfiled_derivation_edge(world, tool, capsys):
    w = world
    A, B, C, rep, labels = _bulk_deriv_world(w)
    rc, out, err = apply_(w, tool, capsys, rep=rep, labels=labels, hand=HAND10)
    assert rc == 0, out + err
    led = ledger(w)
    assert w.rels(*A) == ["cites"]
    assert _entries_of(led, A) == [("d", "needs-seat-read")]       # no phase-c entry, nothing applied
    assert not [e for e in led["entries"] if e["status"] == "applied"
                and (e["source_id"], e["target_id"]) == A]
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [A[0]]
    assert w.eid(*A) not in [n for n, _ in w.retype_reasons()]      # the engine never saw it


def test_f1_labelled_edge_in_an_eligible_stratum_is_acted_on_by_phase_d_per_its_label(world, tool, capsys):
    w = world
    A, B, C, rep, labels = _bulk_deriv_world(w)
    apply_(w, tool, capsys, rep=rep, labels=labels, hand=HAND10)
    e = by_edge(ledger(w))[B]
    assert (e["phase"], e["to"], e["reason"], e["status"]) == ("d", "about", "correct-about", "applied")
    assert w.rels(*B) == ["about"]


def test_f1_unlabelled_edge_in_the_same_stratum_is_still_bulk_retyped(world, tool, capsys):
    w = world
    A, B, C, rep, labels = _bulk_deriv_world(w)
    apply_(w, tool, capsys, rep=rep, labels=labels, hand=HAND10)
    e = by_edge(ledger(w))[C]
    assert (e["phase"], e["to"], e["reason"], e["status"]) == ("c", "about", "bulk:derivation->goal", "applied")
    assert w.rels(*C) == ["about"]


def test_f1_dry_run_plans_no_bulk_retype_for_the_labelled_edge_and_warns_and_counts(world, tool, capsys):
    w = world
    A, B, C, rep, labels = _bulk_deriv_world(w)
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, rep=rep, labels=labels, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    plan = json.loads(dest.read_text())
    assert _entries_of(plan, A) == [("d", "needs-seat-read")]
    assert _entries_of(plan, C) == [("c", "planned")]
    # WARN-and-list, not refuse: the two labelled edges in the eligible stratum
    assert plan["header"]["labelled_in_bulk_strata"] == 2
    assert "WARNING" in err and "bulk" in err
    assert A[0] in err and B[0] in err and C[0] not in err
    assert [g["source_id"] for g in plan["refile_by_source"]] == [A[0]]


def test_f1_header_count_is_zero_when_no_labelled_edge_is_in_an_eligible_stratum(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal")
    labels = {"edges": [lentry(w, s, t, [lab("correct-about")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0
    assert ledger(w)["header"]["labelled_in_bulk_strata"] == 0
    assert "WARNING" not in err


def test_f1_phase_a_does_not_retype_a_labelled_misfiled_derivation_edge_either(world, tool, capsys):
    w = world
    s, t = w.cites("derivation", "goal")
    w.edge("tensions", s, t)                  # a typed duplicate that phase a would take
    rc, out, err = apply_(w, tool, capsys, labels={"edges": [lentry(w, s, t, [_mf_label()])]})
    assert rc == 0, out + err
    assert w.rels(s, t) == ["cites", "tensions"]
    assert _entries_of(ledger(w), (s, t)) == [("d", "needs-seat-read")]
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [s]


def test_f1_phase_a_direct_premise_duplicate_still_ends_as_already_covered(world, tool, capsys):
    w = world
    s, t = w.cites("derivation", "goal")
    w.edge("derives_from", s, t)              # the premise is already wired
    rc, out, err = apply_(w, tool, capsys, labels={"edges": [lentry(w, s, t, [_mf_label()])]})
    assert rc == 0, out + err
    assert _entries_of(ledger(w), (s, t)) == [("d", "applied")]
    assert by_edge(ledger(w))[(s, t)]["reason"] == "already-covered"
    assert w.rels(s, t) == ["about", "derives_from"]


# ---------------------------------------------------------------------------
# F2 -- refile list entries are exactly the edges that stay `cites`
# ---------------------------------------------------------------------------

def test_f2_retyped_derivation_relies_edge_is_not_on_the_refile_list(world, tool, capsys):
    w = world
    s, t = w.cites("derivation", "goal")
    s2, t2 = w.cites("derivation", "goal")        # control: a real refile entry
    labels = {"edges": [
        lentry(w, s, t, [lab("correct-about", premise="relies", reason="names it", labeller="L1"),
                         lab("correct-about", labeller="L2")]),
        lentry(w, s2, t2, [_mf_label()])]}
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, labels=labels, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    plan = json.loads(dest.read_text())
    assert [g["source_id"] for g in plan["refile_by_source"]] == [s2]      # dry run
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    assert w.rels(s, t) == ["about"] and w.rels(s2, t2) == ["cites"]
    refile = _read(w, "refile-by-source")
    assert [g["source_id"] for g in refile] == [s2]
    assert ledger(w)["header"]["refile_counts"] == {"edges": 1, "sources": 1}
    # every edge on the refile list is still a cites edge in the graph
    assert all("cites" in w.rels(g["source_id"], x["target_id"]) for g in refile for x in g["targets"])
    # chosen: the retyped edge stays visible on the promotion list, flagged
    promo = {(p["source_id"], p["target_id"]): p for p in _read(w, "promotion-candidates")}
    assert promo[(s, t)]["retyped"] == "about" and promo[(s, t)]["relies"] is True
    assert (s2, t2) not in promo


# ---------------------------------------------------------------------------
# F3 -- the keeper pair is on no export list; it appears only as a refusal
# ---------------------------------------------------------------------------

def _keeper_labelled(w):
    s, t = w.cites("observation_factual", "evidence")
    labels = {"edges": [lentry(w, s, t, [_mf_label(rel="supported_by")])]}
    return s, t, labels


def _assert_keeper_only_refused(es, pair):
    mine = [e for e in es if (e["source_id"], e["target_id"]) == pair]
    assert [(e["reason"], e["status"]) for e in mine] == [("keeper", "refused:not-loose-cites")]


def test_f3_keeper_labelled_misfiled_and_relies_is_on_no_list_apply(world, tool, capsys):
    w = world
    s, t, labels = _keeper_labelled(w)
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    assert w.rels(s, t) == ["cites"] and w.retype_reasons() == []
    led = ledger(w)
    _assert_keeper_only_refused(led["entries"], (s, t))
    assert _read(w, "promotion-candidates") == [] and _read(w, "refile-by-source") == []
    assert _read(w, "already-covered") == []
    assert _read(w, "lost-relation")["header"] == {"total": 0, "per_stratum": {}}
    h = led["header"]
    assert h["promotion_candidate_count"] == 0
    assert h["refile_counts"] == {"edges": 0, "sources": 0}
    assert h["already_covered_counts"] == {"edges": 0, "sources": 0}
    assert "retype cites -> None" not in out


def test_f3_keeper_labelled_misfiled_and_relies_is_on_no_list_dry_run(world, tool, capsys):
    w = world
    s, t, labels = _keeper_labelled(w)
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, labels=labels, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    plan = json.loads(dest.read_text())
    _assert_keeper_only_refused(plan["entries"], (s, t))
    for k in ("promotion_candidates", "refile_by_source", "already_covered"):
        assert plan[k] == [], k
    assert plan["lost_relation"]["strata"] == {}
    assert not [e for e in plan["entries"] if e["status"] in ("planned", "applied")]
    assert "WOULD retype" not in out
    assert plan["header"]["promotion_candidate_count"] == 0


# ---------------------------------------------------------------------------
# F4 -- the audit gate aborts on a larger cycle count or a genuinely new
# cyclic component, not on a different back-edge path through old nodes
# ---------------------------------------------------------------------------

def test_f4_a_preexisting_cycle_whose_listed_path_changes_does_not_abort(world, tool, capsys):
    w = world
    n1, n2, n3 = (w.node("lesson") for _ in range(3))
    w.edge("cites", n1, n2)
    w.edge("derives_from", n2, n3)
    w.edge("derives_from", n3, n1)
    w.edge("derives_from", n1, n3)
    labels = {"edges": [lentry(w, n1, n2, [lab("correct-about")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    assert w.rels(n1, n2) == ["about"]
    led = ledger(w)
    assert led["header"]["status"] == "complete"
    a = led["header"]["audit"]
    assert a["before"]["cycle_count"] == 1 and a["final"]["cycle_count"] == 1
    # the listed path really did change (this is the shape the abort used to trip on)
    assert a["before"]["cycles"] != a["final"]["cycles"]


def test_f4_a_fake_same_count_all_nodes_already_cyclic_different_path_does_not_abort(
        world, tool, capsys, monkeypatch):
    plant_phases(world)
    sub = {"family": "grounding", "path": ["zz_a", "zz_a"]}
    _fake_audit(monkeypatch, [([CYC], 1), ([CYC], 1), ([sub], 1)])
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 0, out + err
    # (plant_phases leaves one loose cites in an ineligible stratum, so G3's status)
    assert ledger(world)["header"]["status"] == "complete-with-remaining"


# (the fake-listing "new node at equal count" cell moved to the G1 cells below:
# the node-set test is gone, a new cyclic component is now read off the graph)


def test_f4_c_a_larger_cycle_count_aborts_even_when_every_node_was_already_cyclic(
        world, tool, capsys, monkeypatch):
    plant_phases(world)
    sub = {"family": "grounding", "path": ["zz_a", "zz_a"]}
    _fake_audit(monkeypatch, [([CYC], 1), ([CYC], 1), ([sub], 2)])
    rc, out, err = apply_(world, tool, capsys, rep=plant_report())
    assert rc == 3, out + err
    assert ledger(world)["header"]["status"] == "aborted-audit"


# (the fake cross-family cell moved to test_g1_c2: the family is read off the graph)


def test_f4_docstring_states_the_cap_limitation():
    import engram_dag_conformance as dagc
    src = TOOL_PATH.read_text()
    assert str(dagc.MAX_CYCLES_PER_FAMILY) in src and "uncapped" in src.lower()


# ---------------------------------------------------------------------------
# F5 -- an unexpected error after the first retype still writes the ledger
# ---------------------------------------------------------------------------

def test_f5_error_after_phases_a_and_b_committed_still_writes_ledger_and_files(
        world, tool, capsys, monkeypatch):
    w = world
    plant_phases(w)
    before = w.edge_map()

    def boom(self, conn):
        raise RuntimeError("injected phase c failure")

    monkeypatch.setattr(tool.Run, "phase_c", boom)
    rc, out, err = apply_(w, tool, capsys, rep=plant_report())
    assert rc == 1, out + err
    assert "injected phase c failure" in err
    led = ledger(w)
    assert led["header"]["status"] == "aborted-error"
    assert "injected phase c failure" in led["header"]["error"]
    after = w.edge_map()
    changed = {i: after[i][2] for i in after if after[i][2] != before[i][2]}
    applied = {e["edge_id"]: e["to"] for e in led["entries"] if e["status"] == "applied"}
    assert changed and applied == changed
    assert {e["phase"] for e in led["entries"]} == {"a", "b"}
    d = w.home / "migrations" / "cites-step3"
    for prefix in ("refile-by-source", "promotion-candidates", "already-covered",
                   "lost-relation", "needs-seat-read"):
        assert list(d.glob(f"{prefix}-*.json")), prefix
    for p in d.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# F6 -- a dry run after an apply does not list already-retyped edges
# ---------------------------------------------------------------------------

def test_f6_dry_run_after_apply_lists_nothing_as_would_retype(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal")
    labels = {"edges": [lentry(w, s, t, [lab("correct-about")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0 and w.rels(s, t) == ["about"]
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, labels=labels, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    assert "WOULD retype" not in out
    plan = json.loads(dest.read_text())
    assert _entries_of(plan, (s, t)) == [("d", "skipped-not-cites")]
    assert not [e for e in plan["entries"] if e["status"] == "planned"]


# ---------------------------------------------------------------------------
# F7 -- the hand-check counts DISTINCT edge ids
# ---------------------------------------------------------------------------

def test_f7_a_repeated_id_does_not_satisfy_the_hand_check_minimum(world, tool, capsys):
    rep = report(strata=[stratum("derivation", "goal", {"correct-about": 30})])
    _refused(world, tool, capsys, ["edges_read", "distinct"], rep=rep, labels={"edges": []},
             hand=handcheck(edges_read=[7] * 25, agree=25), apply=True)


def test_f7_duplicates_beside_enough_distinct_ids_still_pass(world, tool, capsys):
    rep = report(strata=[stratum("derivation", "goal", {"correct-about": 30})])
    rc, out, err = apply_(world, tool, capsys, rep=rep,
                          hand=handcheck(edges_read=list(range(20)) + [0] * 5, agree=25))
    assert rc == 0, out + err


def test_f7_non_scalar_edge_ids_are_a_refusal_not_a_crash(world, tool, capsys):
    _refused(world, tool, capsys, ["edges_read"], rep=report(), labels={"edges": []},
             hand=handcheck(edges_read=[[1], {"a": 1}], agree=2), apply=True)


# ---------------------------------------------------------------------------
# F8 -- --help, --plan-out over an existing file, the K1 bound, URI escaping
# ---------------------------------------------------------------------------

def test_f8_help_exits_zero(tool, capsys):
    assert tool.main(["--help"]) == 0
    out, err = capsys.readouterr()
    assert "usage" in out.lower() and "--report" in out


def test_f8_a_bad_flag_is_still_a_refusal(tool, capsys):
    assert tool.main(["--no-such-flag"]) == 2


def test_f8_plan_out_onto_an_existing_file_is_a_precise_refusal_exit_2(world, tool, capsys):
    w = world
    plant_phases(w)
    dest = w.inputs / "plan.json"
    dest.write_text("keep me")
    snap = w.dump()
    rc, out, err = invoke(w, tool, capsys, rep=plant_report(), extra=["--plan-out", str(dest)])
    assert rc == 2, out + err
    assert "already exists" in err and "plan.json" in err
    assert dest.read_text() == "keep me" and w.dump() == snap


def test_f8_dry_run_leaves_the_wal_database_main_file_bytes_unchanged(world, tool, capsys):
    w = world
    plant_phases(w)
    c = w.conn()
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    db = w.home / "knowledge.db"
    h0 = hashlib.sha256(db.read_bytes()).hexdigest()
    rc, out, err = invoke(w, tool, capsys, rep=plant_report(), labels={"edges": []})
    assert rc == 0, out + err
    assert hashlib.sha256(db.read_bytes()).hexdigest() == h0
    assert "WOULD retype" in out          # it did read the graph


def test_f8_read_only_open_survives_hash_and_question_mark_in_the_data_path(tmp_path, monkeypatch, tool, capsys):
    w = _build_world(tmp_path, monkeypatch, dirname="h#a?b%c d")
    s, t = w.cites("lesson", "goal")
    labels = {"edges": [lentry(w, s, t, [lab("correct-about")])]}
    rc, out, err = invoke(w, tool, capsys, rep=report(), labels=labels)
    assert rc == 0, out + err
    assert "WOULD retype" in out and s in out


# ---------------------------------------------------------------------------
# F9 -- two gaps: lost-relation covers every label kind; the premise walk's
# visited set (not only the hop cap) keeps a lattice linear
# ---------------------------------------------------------------------------

def test_f9a_lost_relation_lists_misfiled_and_unforeseen_labels_naming_non_addable_relations(world, tool, capsys):
    w = world
    s1, t1 = w.cites("lesson", "goal")
    s2, t2 = w.cites("lesson", "goal")
    labels = {"edges": [
        lentry(w, s1, t1, [lab("mis-filed-premise", "supported_by", premise="relies", reason="premise quote")]),
        lentry(w, s2, t2, [lab("unforeseen", "resolves", reason="a new kind that resolves it")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    doc = _lost(w)
    got = _lost_pairs(doc)
    assert set(got) == {(s1, t1), (s2, t2)}
    assert got[(s1, t1)]["relation_named"] == "supported_by"
    assert got[(s2, t2)]["relation_named"] == "resolves"
    assert doc["header"] == {"total": 2, "per_stratum": {"lesson->goal": 2}}


def test_f9b_diamond_lattice_premise_walk_is_linear_with_the_visited_set(world, tool):
    w = world
    levels = 14                                  # 2**14 paths, well under the hop cap
    assert levels < tool.MAX_ANCESTRY_HOPS
    src = w.node("derivation")
    layers = [[src]]
    for _ in range(levels):
        layers.append([w.node("derivation"), w.node("derivation")])
    for upper, lower in zip(layers, layers[1:]):
        for u in upper:
            for v in lower:
                w.edge("derives_from", u, v)
    loner = w.node("derivation")                 # unreachable: the walk must exhaust the lattice
    stats = {}
    conn = w.conn()
    try:
        assert tool.premise_path(conn, src, loner, stats) is None
        assert stats["expanded"] <= 2 * levels + 2, stats          # linear, not 2**levels
        stats2 = {}
        deep = layers[-1][0]
        path = tool.premise_path(conn, src, deep, stats2)
        assert path[0] == src and path[-1] == deep and len(path) == levels + 1
        assert stats2["expanded"] <= 2 * levels + 2
    finally:
        conn.close()


# ===========================================================================
# Review round 2 folds (G1-G5)
# ===========================================================================

# ---------------------------------------------------------------------------
# G1 -- the audit gate compares strongly connected components, not the
# (capped) listing
# ---------------------------------------------------------------------------

def _cycle_groups(w, n):
    """n disjoint 3-node grounding cycles: a loose cites p -> q closed by two
    derives_from edges q -> r -> p. No typed edge sits on the (p, q) pair, so
    phase a never selects them; the graph has n cycles but the audit lists 20."""
    groups = []
    for _ in range(n):
        p, q, r = w.node("lesson"), w.node("goal"), w.node("lesson")
        w.edge("cites", p, q)
        w.edge("derives_from", q, r)
        w.edge("derives_from", r, p)
        groups.append((p, q, r))
    return groups


def _labels_for(w, groups, k):
    return {"edges": [lentry(w, p, q, [lab("correct-about")]) for p, q, _r in groups[:k]]}


def _after_first_retype(monkeypatch, action):
    """Run `action(conn)` once, right after the engine commits its first retype
    (the graph then gains raw edges no guard saw: a plausible audit regression)."""
    import engram_cornerstone
    real = engram_cornerstone._retype_edge_impl
    state = {"done": False}

    def wrapped(*a, **kw):
        out = real(*a, **kw)
        if not state["done"]:
            state["done"] = True
            c = engram_cornerstone.core._get_db()
            try:
                action(c)
                c.commit()
            finally:
                c.close()
        return out

    monkeypatch.setattr(engram_cornerstone, "_retype_edge_impl", wrapped)


def _raw_edge(conn, core, rel, s, t):
    conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
                 (s, t, rel, core._now()))


def test_g1_a_capped_listing_whose_count_falls_and_listing_changes_does_not_abort(
        world, tool, capsys):
    import engram_dag_conformance as dagc
    w = world
    groups = _cycle_groups(w, 25)
    assert len(groups) > dagc.MAX_CYCLES_PER_FAMILY
    rc, out, err = apply_(w, tool, capsys, labels=_labels_for(w, groups, 5))
    assert rc == 0, out + err
    led = ledger(w)
    a = led["header"]["audit"]
    assert a["before"]["cycle_count"] == 25 and len(a["before"]["cycles"]) == 20
    assert a["final"]["cycle_count"] == 20 and len(a["final"]["cycles"]) == 20
    before_nodes = {n for c in a["before"]["cycles"] for n in c["path"]}
    after_nodes = {n for c in a["final"]["cycles"] for n in c["path"]}
    assert after_nodes - before_nodes          # the listing really did change (the old trap)
    assert led["header"]["status"] != "aborted-audit"
    assert len(entries(led, status="applied")) == 5


def test_g1_b_a_phase_that_raises_the_uncapped_count_aborts(world, tool, capsys, monkeypatch):
    w = world
    groups = _cycle_groups(w, 25)
    n1, n2, n3 = (w.node("lesson") for _ in range(3))
    w.edge("derives_from", n1, n2); w.edge("derives_from", n2, n3); w.edge("derives_from", n3, n1)
    # two extra chords inside the one component: every node was already cyclic,
    # the count rises by 2, one retyped cites removes 1
    _after_first_retype(monkeypatch, lambda c: (
        _raw_edge(c, w.core, "derives_from", n3, n2), _raw_edge(c, w.core, "derives_from", n2, n1)))
    rc, out, err = apply_(w, tool, capsys, labels=_labels_for(w, groups, 1))
    assert rc == 3, out + err
    led = ledger(w)
    assert led["header"]["status"] == "aborted-audit"
    assert led["header"]["audit"]["before"]["cycle_count"] == 26
    assert led["header"]["audit"]["after_phase"]["d"]["cycle_count"] == 27


def test_g1_c_a_new_cyclic_component_aborts_even_with_more_than_20_other_cycles(
        world, tool, capsys, monkeypatch):
    w = world
    groups = _cycle_groups(w, 25)
    f1, f2 = w.node("lesson"), w.node("lesson")      # on no cycle before
    # one cycle goes (the retyped cites), one new cycle appears: count 25 -> 25
    _after_first_retype(monkeypatch, lambda c: (
        _raw_edge(c, w.core, "derives_from", f1, f2), _raw_edge(c, w.core, "derives_from", f2, f1)))
    rc, out, err = apply_(w, tool, capsys, labels=_labels_for(w, groups, 1))
    assert rc == 3, out + err
    led = ledger(w)
    assert led["header"]["status"] == "aborted-audit"
    a = led["header"]["audit"]
    assert a["before"]["cycle_count"] == a["after_phase"]["d"]["cycle_count"] == 25
    assert f1 in out + err and f2 in out + err          # the new component is named


def test_g1_c2_a_node_cyclic_in_another_family_is_new_for_this_family(
        world, tool, capsys, monkeypatch):
    w = world
    groups = _cycle_groups(w, 3)
    p2, q2, _r2 = groups[1]
    # the retyped cites removes one grounding cycle; an abstraction cycle appears
    # through nodes that are only on a GROUNDING cycle: count equal, family new
    _after_first_retype(monkeypatch, lambda c: (
        _raw_edge(c, w.core, "generalizes", p2, q2), _raw_edge(c, w.core, "generalizes", q2, p2)))
    rc, out, err = apply_(w, tool, capsys, labels=_labels_for(w, groups, 1))
    assert rc == 3, out + err
    assert ledger(w)["header"]["status"] == "aborted-audit"


def test_g1_d_the_reviewers_earlier_repro_still_does_not_abort(world, tool, capsys):
    w = world
    n1, n2, n3 = (w.node("lesson") for _ in range(3))
    w.edge("cites", n1, n2)
    w.edge("derives_from", n2, n3)
    w.edge("derives_from", n3, n1)
    w.edge("derives_from", n1, n3)
    rc, out, err = apply_(w, tool, capsys, labels={"edges": [lentry(w, n1, n2, [lab("correct-about")])]})
    assert rc == 0, out + err
    assert w.rels(n1, n2) == ["about"]


def test_g1_cyclic_nodes_unit_self_loop_pair_and_chain(tool):
    edges = [("a", "b"), ("b", "a"), ("c", "c"), ("d", "e"), ("e", "f")]
    assert tool.cyclic_nodes(edges) == {"a", "b", "c"}
    assert tool.cyclic_nodes([]) == set()
    long = [(f"n{i}", f"n{i + 1}") for i in range(3000)]          # iterative: no recursion limit
    assert tool.cyclic_nodes(long) == set()
    assert tool.cyclic_nodes(long + [("n3000", "n0")]) == {f"n{i}" for i in range(3001)}


def test_g1_docstring_names_the_scc_rule_and_drops_the_cap_caveat():
    src = TOOL_PATH.read_text()
    assert "strongly connected" in src.lower()
    assert "beyond the cap" not in src.lower()


# ---------------------------------------------------------------------------
# G2 -- --apply takes a backup first and refuses to start without one
# ---------------------------------------------------------------------------

def _backups(d):
    return sorted(Path(d).glob("knowledge-before-cites-step3-*.db"))


def _small_apply_world(w):
    s, t = w.cites("lesson", "goal")
    return s, t, {"edges": [lentry(w, s, t, [lab("correct-about")])]}


def test_g2_backup_exists_opens_matches_the_graph_before_any_retype_and_is_0600(
        world, tool, capsys):
    w = world
    plant_phases(w)
    before = w.edge_map()
    rc, out, err = apply_(w, tool, capsys, rep=plant_report())
    assert rc == 0, out + err
    d = w.home / "migrations" / "cites-step3"
    (bk,) = _backups(d)
    assert stat.S_IMODE(bk.stat().st_mode) == 0o600
    conn = sqlite3.connect(f"file:{bk}?mode=ro", uri=True)
    try:
        got = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT id, source_id, target_id, relation FROM edges")}
    finally:
        conn.close()
    assert got == before                       # the pre-retype graph, all edges, all relations
    assert w.edge_map() != before              # and the live graph did change
    h = ledger(w)["header"]
    assert h["backup"] == {"path": str(bk), "bytes": bk.stat().st_size, "edges": len(before)}
    assert str(bk) in out and "backup" in out.lower()


def test_g2_backup_goes_to_out_dir_when_given(world, tool, capsys):
    w = world
    s, t, labels = _small_apply_world(w)
    dest = w.inputs / "elsewhere"
    rc, out, err = apply_(w, tool, capsys, labels=labels, extra=["--out-dir", str(dest)])
    assert rc == 0, out + err
    assert len(_backups(dest)) == 1


def test_g2_a_failing_backup_refuses_exit_2_and_nothing_is_written(
        world, tool, capsys, monkeypatch):
    w = world
    s, t, labels = _small_apply_world(w)
    snap = w.dump()
    spy_calls = []
    import engram_cornerstone
    real = engram_cornerstone._retype_edge_impl
    monkeypatch.setattr(engram_cornerstone, "_retype_edge_impl",
                        lambda *a, **kw: (spy_calls.append(kw), real(*a, **kw))[1])

    def boom(*a, **kw):
        raise OSError("simulated: no space left on device")

    monkeypatch.setattr(tool, "_backup_db", boom)
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 2, out + err
    msg = out + err
    assert "backup" in msg.lower() and "no space left" in msg
    assert w.dump() == snap and spy_calls == [] and w.retype_reasons() == []
    assert not list((w.home / "migrations").rglob("ledger-*.json"))
    assert not _backups(w.home / "migrations" / "cites-step3")
    assert w.rels(s, t) == ["cites"]


def test_g2_an_unwritable_out_dir_is_the_same_refusal(world, tool, capsys):
    w = world
    s, t, labels = _small_apply_world(w)
    snap = w.dump()
    ro = w.inputs / "readonly"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        rc, out, err = apply_(w, tool, capsys, labels=labels, extra=["--out-dir", str(ro)])
    finally:
        os.chmod(ro, 0o700)
    assert rc == 2, out + err
    assert "backup" in (out + err).lower()
    assert w.dump() == snap and w.rels(s, t) == ["cites"] and w.retype_reasons() == []
    assert list(ro.iterdir()) == []


def test_g2_a_backup_whose_edge_count_differs_is_refused(world, tool, capsys, monkeypatch):
    w = world
    s, t, labels = _small_apply_world(w)
    snap = w.dump()

    def short_backup(src, dest):
        c = sqlite3.connect(dest)
        c.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY)")
        c.commit()
        c.close()

    monkeypatch.setattr(tool, "_backup_db", short_backup)
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 2, out + err
    assert "edge" in (out + err).lower() and "backup" in (out + err).lower()
    assert w.dump() == snap and w.retype_reasons() == []


def test_g2_a_dry_run_takes_no_backup(world, tool, capsys):
    w = world
    s, t, labels = _small_apply_world(w)
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, labels=labels, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    assert not list(w.home.rglob("knowledge-before-cites-step3-*"))
    assert "backup" not in json.loads(dest.read_text())["header"]


# ---------------------------------------------------------------------------
# G3 -- a finished run must not look like a finished migration
# ---------------------------------------------------------------------------

def _set_status(w, node, status):
    c = w.conn()
    try:
        c.execute("UPDATE nodes SET status=? WHERE id=?", (status, node))
        c.commit()
    finally:
        c.close()


def test_g3_unselected_loose_edges_are_counted_by_stratum_and_the_status_says_so(world, tool, capsys):
    w = world
    s1, t1 = w.cites("lesson", "goal")                      # selected (labelled)
    s2, t2 = w.cites("lesson", "goal", src_current=0)       # unselected, superseded source
    s3, t3 = w.cites("lesson", "goal", tgt_current=0)       # unselected, superseded target
    s4, t4 = w.cites("task", "goal")                        # unselected, retracted target
    _set_status(w, t4, "retracted")
    w.cites("observation_factual", "evidence")              # the keeper pair: never counted
    labels = {"edges": [lentry(w, s1, t1, [lab("correct-about")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    h = ledger(w)["header"]
    assert h["loose_cites_remaining"] == {
        "total": 3, "per_stratum": {"lesson->goal": 2, "task->goal": 1},
        "non_current_source": 1, "non_current_target": 2}
    assert h["status"] == "complete-with-remaining"
    assert "loose_cites_remaining" in out and "complete-with-remaining" in out


def test_g3_everything_selected_is_complete_with_a_zero_count(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal")
    w.cites("observation_factual", "evidence")              # the keeper pair is not "remaining"
    rc, out, err = apply_(w, tool, capsys, labels={"edges": [lentry(w, s, t, [lab("correct-about")])]})
    assert rc == 0, out + err
    h = ledger(w)["header"]
    assert h["loose_cites_remaining"] == {"total": 0, "per_stratum": {},
                                          "non_current_source": 0, "non_current_target": 0}
    assert h["status"] == "complete"


def test_g3_the_dry_run_projects_the_figure_after_the_planned_retypes(world, tool, capsys):
    w = world
    s1, t1 = w.cites("lesson", "goal")
    w.cites("lesson", "goal", src_current=0)
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, labels={"edges": [lentry(w, s1, t1, [lab("correct-about")])]},
                          extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    h = json.loads(dest.read_text())["header"]
    assert h["loose_cites_remaining"] == {"total": 1, "per_stratum": {"lesson->goal": 1},
                                          "non_current_source": 1, "non_current_target": 0}
    assert "loose_cites_remaining" in out
    assert w.rels(s1, t1) == ["cites"]                      # still a dry run


def test_g3_an_aborted_run_is_never_relabelled_complete_with_remaining(
        world, tool, capsys, monkeypatch):
    w = world
    groups = _cycle_groups(w, 2)
    w.cites("lesson", "goal", src_current=0)
    f1, f2 = w.node("lesson"), w.node("lesson")
    _after_first_retype(monkeypatch, lambda c: (
        _raw_edge(c, w.core, "derives_from", f1, f2), _raw_edge(c, w.core, "derives_from", f2, f1)))
    rc, out, err = apply_(w, tool, capsys, labels=_labels_for(w, groups, 1))
    assert rc == 3
    h = ledger(w)["header"]
    assert h["status"] == "aborted-audit" and h["loose_cites_remaining"]["total"] == 2


# ---------------------------------------------------------------------------
# G4 -- a cites duplicating an existing about can never be removed: its own bucket
# ---------------------------------------------------------------------------

BUCKET = "duplicate-of-about-unremovable"


def _dup_about(w, stype="lesson", ttype="goal"):
    s, t = w.cites(stype, ttype)
    w.edge("about", s, t)
    return s, t


def test_g4_two_in_phase_a_and_one_in_bulk_land_in_the_bucket(world, tool, capsys):
    w = world
    a1, a2 = _dup_about(w), _dup_about(w, "task", "goal")
    b1 = _dup_about(w, "definition", "goal")
    rep = report(strata=[stratum("definition", "goal", {"correct-about": 10})])
    snap_pairs = [a1, a2, b1]
    # bulk first (phase a would claim b1 as a typed duplicate): its own run
    rc, out, err = apply_(w, tool, capsys, rep=rep, extra=["--phases", "c"])
    assert rc == 0, out + err
    h = ledger(w)["header"]
    assert h[BUCKET] == {"count": 1, "entries": [
        {"edge_id": w.eid(*b1), "source_id": b1[0], "target_id": b1[1]}]}
    e = by_edge(ledger(w))[b1]
    assert e["status"].startswith("refused:") and e["phase"] == "c" and e["bucket"] == BUCKET
    rc, out, err = apply_(w, tool, capsys, rep=rep, extra=["--phases", "a"])
    assert rc == 0, out + err
    led = ledger(w)
    got = led["header"][BUCKET]
    assert got["count"] == 3                    # the bulk one is refused again, now by phase a
    assert {(x["source_id"], x["target_id"]) for x in got["entries"]} == set(snap_pairs)
    assert all(set(x) == {"edge_id", "source_id", "target_id"} for x in got["entries"])
    assert [(x["edge_id"]) for x in got["entries"]] == sorted(x["edge_id"] for x in got["entries"])
    for p in snap_pairs:
        assert "cites" in w.rels(*p)            # still loose: nothing removed
    assert BUCKET in out and a1[0] in out


def test_g4_the_dry_run_plan_lists_them_too(world, tool, capsys):
    w = world
    a1 = _dup_about(w)
    s, t = w.cites("lesson", "goal")            # an ordinary planned retype
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, labels={"edges": [lentry(w, s, t, [lab("correct-about")])]},
                          extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    plan = json.loads(dest.read_text())
    assert plan["header"][BUCKET] == {"count": 1, "entries": [
        {"edge_id": w.eid(*a1), "source_id": a1[0], "target_id": a1[1]}]}
    assert BUCKET in out
    assert plan["header"]["loose_cites_remaining"]["total"] == 1   # the unremovable dup stays


def test_g4_an_unrelated_refusal_does_not_land_in_the_bucket(world, tool, capsys, monkeypatch):
    w = world
    s, t = w.cites("feeling_report", "goal")
    import engram_cornerstone
    monkeypatch.setattr(engram_cornerstone, "_retype_edge_impl",
                        lambda *a, **kw: json.dumps({"error": "endpoint rule: not allowed"}))
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    h = ledger(w)["header"]
    assert h[BUCKET] == {"count": 0, "entries": []}
    assert by_edge(ledger(w))[(s, t)]["status"].startswith("refused:")
    assert "bucket" not in by_edge(ledger(w))[(s, t)]


def test_g4_the_engines_wording_without_a_real_about_edge_is_not_enough(
        world, tool, capsys, monkeypatch):
    w = world
    s, t = w.cites("feeling_report", "goal")
    w.edge("about", t, s)                       # an about in the OTHER direction is no duplicate
    import engram_cornerstone
    monkeypatch.setattr(engram_cornerstone, "_retype_edge_impl", lambda *a, **kw: json.dumps({
        "error": f"a 'about' edge from {s} to {t} already exists -- nothing to retype into. x"}))
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert ledger(w)["header"][BUCKET] == {"count": 0, "entries": []}


def test_g4_a_real_about_edge_with_a_different_refusal_is_not_enough_either(
        world, tool, capsys, monkeypatch):
    w = world
    s, t = _dup_about(w, "feeling_report", "goal")
    import engram_cornerstone
    monkeypatch.setattr(engram_cornerstone, "_retype_edge_impl",
                        lambda *a, **kw: json.dumps({"error": "an unrelated guard refused this"}))
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert ledger(w)["header"][BUCKET] == {"count": 0, "entries": []}


# ---------------------------------------------------------------------------
# G5 -- hand-check ids are edges of the report; is_named is a word match
# ---------------------------------------------------------------------------

def test_g5a_edges_read_must_be_edge_ids_of_the_report(world, tool, capsys):
    w = world
    plant_phases(w)
    rep = report(edges=[redge(i, f"a{i}", f"b{i}", "lesson", "goal", "correct-about") for i in (1, 2, 3)])
    snap = w.dump()
    rc, out, err = invoke(w, tool, capsys, rep=rep, labels={"edges": []}, apply=True, pad=False,
                          hand=handcheck(edges_read=[1, 2, 999, 998], agree=4))
    assert rc == 2, out + err
    assert "999" in err and "998" in err
    assert w.dump() == snap and not (w.home / "migrations").exists()
    rc, out, err = invoke(w, tool, capsys, rep=rep, labels={"edges": []}, apply=True, pad=False,
                          hand=handcheck(edges_read=[1, 2, 3], agree=3))
    assert rc == 0, out + err


def test_g5a_the_minimum_must_come_from_labelled_derivation_correct_about_edges(world, tool, capsys):
    w = world
    d_edges = [redge(i, f"d{i}", f"g{i}", "derivation", "goal", "correct-about") for i in (10, 11, 12)]
    o_edges = [redge(i, f"o{i}", f"g{i}", "lesson", "goal", "correct-about") for i in (20, 21, 22)]
    rep = report(strata=[stratum("derivation", "goal", {"correct-about": 3})], edges=d_edges + o_edges)
    rc, out, err = invoke(w, tool, capsys, rep=rep, labels={"edges": []}, apply=True, pad=False,
                          hand=handcheck(edges_read=[20, 21, 22], agree=3))
    assert rc == 2 and "derivation" in err, out + err
    rc, out, err = invoke(w, tool, capsys, rep=rep, labels={"edges": []}, apply=True, pad=False,
                          hand=handcheck(edges_read=[10, 11, 12], agree=3))
    assert rc == 0, out + err
    rc, out, err = invoke(w, tool, capsys, rep=rep, labels={"edges": []}, apply=True, pad=False,
                          hand=handcheck(edges_read=[10, 11, 20, 21, 22], agree=5))
    assert rc == 2, out + err                        # only 2 of the 3 required come from them


def test_g5a_unique_id_rule_is_kept(world, tool, capsys):
    rep = report(edges=[redge(1, "a", "b", "lesson", "goal", "correct-about")])
    rc, out, err = invoke(world, tool, capsys, rep=rep, labels={"edges": []}, apply=True, pad=False,
                          hand=handcheck(edges_read=[1, 1, 1], agree=3))
    assert rc == 0, out + err                        # one distinct id meets the floor of 1
    rep2 = report(strata=[stratum("derivation", "goal", {"correct-about": 3})],
                  edges=[redge(i, f"d{i}", "g", "derivation", "goal", "correct-about") for i in (1, 2, 3)])
    rc, out, err = invoke(world, tool, capsys, rep=rep2, labels={"edges": []}, apply=True, pad=False,
                          hand=handcheck(edges_read=[1, 1, 1], agree=3))
    assert rc == 2 and "distinct" in err


def _node_with_id(w, nid, typ, claim):
    c = w.conn()
    try:
        c.execute("INSERT INTO nodes (id, type, claim, created_at, is_current, confidence) "
                  "VALUES (?, ?, ?, ?, 1, 0.5)", (nid, typ, claim, OLD))
        c.commit()
    finally:
        c.close()


def test_g5b_a_target_id_inside_a_longer_id_is_not_named(world, tool, capsys):
    w = world
    _node_with_id(w, "ob_01", "goal", "target one")
    _node_with_id(w, "ob_0105", "goal", "a different target")
    _node_with_id(w, "zz_src_a", "lesson", "see ob_0105 for the details")        # names only the longer id
    _node_with_id(w, "zz_src_b", "lesson", "as noted (ob_01), the claim holds")   # names ob_01 exactly
    _node_with_id(w, "zz_src_c", "lesson", "see ob_01.")                         # punctuation boundary
    _node_with_id(w, "zz_src_d", "lesson", "ob_01_x and xob_01 are other ids")    # id characters around it
    for src in ("zz_src_a", "zz_src_b", "zz_src_c", "zz_src_d"):
        w.edge("cites", src, "ob_01")
    labels = {"edges": [lentry(w, src, "ob_01", [lab("cannot-judge")])
                        for src in ("zz_src_a", "zz_src_b", "zz_src_c", "zz_src_d")]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    by = by_edge(ledger(w))
    assert by[("zz_src_a", "ob_01")]["status"] == "applied"
    assert by[("zz_src_a", "ob_01")]["reason"] == "no-textual-trace"
    assert by[("zz_src_b", "ob_01")]["status"] == "needs-seat-read"
    assert by[("zz_src_c", "ob_01")]["status"] == "needs-seat-read"
    assert by[("zz_src_d", "ob_01")]["reason"] == "no-textual-trace"
