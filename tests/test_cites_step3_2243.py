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
  K3  phases a-f in order, each idempotent, every retype through the engine's
      _retype_edge_impl; bulk eligibility RECOMPUTED, never read from the
      report's own decision string.
  K4  DAG audit before and after each phase; a new cycle, or a larger
      cycle_count, aborts with exit 3.
  K5  ledger / candidate / needs-seat-read files, dir 0700, files 0600.
  K6  refusals reported, never worked around; never a provenance edge.
  K7  named-edge mis-filed rate beside every no-textual-trace retype.
  K8  pending cascade marker -> deferred-pending-marker, no fallback.
  K9  a second apply re-applies no edge (a refused or deferred one is retried and recorded again).
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
_DEFAULT_META = object()
DEFAULT_RTYPE = "deductive_modus_ponens"


def rt(rtype):
    """The metadata blob of a derivation of the given reasoning type."""
    return json.dumps({"reasoning_type": rtype})


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

    def node(self, typ, current=1, claim=None, created=OLD, metadata=_DEFAULT_META):
        # evidence nodes carry the `ev_` id prefix the engine's only cites
        # writer requires of a cites target
        nid = f"{'ev' if typ == 'evidence' else 'zz'}_{next(_ctr):06d}"
        # a real derivation always carries a reasoning_type (#2243 hand-only
        # rule); the shared default is the ordinary refile-able one. `metadata`
        # is the raw column value: None -> SQL NULL, a str -> stored verbatim.
        if metadata is _DEFAULT_META:
            metadata = rt(DEFAULT_RTYPE) if typ == "derivation" else None
        c = self.conn()
        try:
            c.execute(
                "INSERT INTO nodes (id, type, claim, created_at, is_current, confidence, metadata) "
                "VALUES (?, ?, ?, ?, ?, 0.5, ?)",
                (nid, typ, claim if claim is not None else f"claim {nid}", created, current, metadata))
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

    def cites(self, stype, ttype, src_current=1, src_claim=None, tgt_current=1,
              src_meta=_DEFAULT_META):
        """A loose (or keeper) cites edge between two fresh nodes. Returns
        (source_id, target_id)."""
        t = self.node(ttype, current=tgt_current, created=OLD)
        s = self.node(stype, current=src_current, created=NEW, claim=src_claim,
                      metadata=src_meta)
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
        # #3882: the store-maintenance lock directory (and its holder file) is a sidecar of the store, like
        # -wal/-shm: the tool creates it beside the store and it stays; it is not an output of the run.
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")
                      if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal"))
                      and ".maint.lockdir" not in p.relative_to(self.home).parts[0])

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
    assert len(calls) == 1 + 6          # baseline + one per phase a..f
    led = ledger(world)
    assert led["header"]["audit"]["before"]["cycle_count"] == 0
    assert len(led["header"]["audit"]["after_phase"]) == 6


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
        # a/b/c/f select from the graph and see nothing; d/e take the labelled
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
    s2, t2 = w.cites("lesson", "goal", src_current=0)       # phase f (frozen-history) takes it
    s3, t3 = w.cites("lesson", "goal", tgt_current=0)       # unselected, superseded target
    s4, t4 = w.cites("task", "goal")                        # unselected, retracted target
    _set_status(w, t4, "retracted")
    w.cites("observation_factual", "evidence")              # the keeper pair: never counted
    labels = {"edges": [lentry(w, s1, t1, [lab("correct-about")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    h = ledger(w)["header"]
    assert h["loose_cites_remaining"] == {
        "total": 2, "per_stratum": {"lesson->goal": 1, "task->goal": 1},
        "non_current_source": 0, "non_current_target": 2}
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
    w.cites("lesson", "goal", src_current=0)                # phase f plans it
    w.cites("lesson", "goal", tgt_current=0)                # current source: stays
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, labels={"edges": [lentry(w, s1, t1, [lab("correct-about")])]},
                          extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    h = json.loads(dest.read_text())["header"]
    assert h["loose_cites_remaining"] == {"total": 1, "per_stratum": {"lesson->goal": 1},
                                          "non_current_source": 0, "non_current_target": 1}
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


# ---------------------------------------------------------------------------
# Phase f (frozen-history): Kepler's ruling, relaying Lei's "no loose cites
# remain". A loose cites whose SOURCE is non-current (superseded or retracted)
# can only ever become `about`: a frozen node carries no premise edge any live
# cascade route or confidence will read again, and a refile retracts and
# refiles the CURRENT node. Phase f runs after phase e and takes every such
# edge no earlier phase touched.
# ---------------------------------------------------------------------------

def _frozen_world(w):
    """lesson->goal is in no report stratum, so it is never bulk-eligible."""
    P = {}
    P["sup"] = w.cites("lesson", "goal", src_current=0)
    P["sup_tgt_sup"] = w.cites("lesson", "goal", src_current=0, tgt_current=0)
    P["retracted"] = w.cites("task", "goal", src_current=0)
    _set_status(w, P["retracted"][0], "retracted")
    return P


def _f_entry(led, pair):
    return [e for e in led["entries"] if (e["source_id"], e["target_id"]) == pair]


def test_phase_f_a_unlabelled_superseded_source_in_a_non_bulk_stratum_becomes_about(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal", src_current=0)
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert w.rels(s, t) == ["about"]
    led = ledger(w)
    (e,) = _f_entry(led, (s, t))
    assert (e["phase"], e["from"], e["to"], e["reason"], e["status"]) == (
        "f", "cites", "about", "frozen-history", "applied")
    assert any(d.get("reason") == "cites-step3 f: frozen-history"
               for _n, d in w.retype_reasons())
    h = led["header"]
    assert h["loose_cites_remaining"] == {"total": 0, "per_stratum": {},
                                          "non_current_source": 0, "non_current_target": 0}
    assert h["status"] == "complete"
    assert h["phases"] == ["a", "b", "c", "d", "e", "f"]


def test_phase_f_b_a_retracted_source_is_taken_too(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal", src_current=0)
    _set_status(w, s, "retracted")
    s2, t2 = w.cites("task", "goal", src_current=1)       # retracted but still flagged current
    _set_status(w, s2, "retracted")
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert w.rels(s, t) == ["about"] and w.rels(s2, t2) == ["about"]
    led = ledger(w)
    for p in ((s, t), (s2, t2)):
        (e,) = _f_entry(led, p)
        assert (e["phase"], e["reason"], e["status"]) == ("f", "frozen-history", "applied")
    assert led["header"]["status"] == "complete"
    assert led["header"]["loose_cites_remaining"]["total"] == 0
    exp = {x["source_id"]: x for x in _read(w, "frozen-history")}
    assert exp[s]["source_status"] == "retracted" and exp[s2]["source_status"] == "retracted"


def test_phase_f_c_current_source_and_superseded_target_alone_are_not_touched(world, tool, capsys):
    w = world
    cur = w.cites("lesson", "goal")                            # current source, current target
    tgt_sup = w.cites("lesson", "goal", tgt_current=0)         # current source, SUPERSEDED target
    sup = w.cites("lesson", "goal", src_current=0)
    sup_id = w.eid(*sup)
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert w.rels(*cur) == ["cites"] and w.rels(*tgt_sup) == ["cites"]
    assert w.rels(*sup) == ["about"]
    led = ledger(w)
    assert not _f_entry(led, cur) and not _f_entry(led, tgt_sup)
    h = led["header"]
    assert h["loose_cites_remaining"]["total"] == 2
    assert h["loose_cites_remaining"]["non_current_source"] == 0
    assert h["loose_cites_remaining"]["non_current_target"] == 1
    assert h["status"] == "complete-with-remaining"
    assert {x["edge_id"] for x in _read(w, "frozen-history")} == {sup_id}


def test_phase_f_d_a_labelled_superseded_source_edge_is_left_to_its_own_list(world, tool, capsys):
    w = world
    # (1) labelled `unforeseen`: phase d lists it for a seat read
    un = w.cites("lesson", "goal", src_current=0)
    # (2) a derivation source labelled mis-filed-premise stays cites on the refile list
    t = w.node("derivation", created=OLD)
    s = w.node("derivation", current=0, created=NEW)
    w.edge("cites", s, t)
    labels = {"edges": [lentry(w, *un, [lab("unforeseen")]),
                        lentry(w, s, t, [lab("mis-filed-premise", "derives_from", premise="relies")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels)
    assert rc == 0, out + err
    assert w.rels(*un) == ["cites"] and w.rels(s, t) == ["cites"]
    led = ledger(w)
    assert [(e["phase"], e["status"]) for e in _f_entry(led, un)] == [("d", "needs-seat-read")]
    assert [(e["phase"], e["status"]) for e in _f_entry(led, (s, t))] == [("d", "needs-seat-read")]
    assert [g["source_id"] for g in _read(w, "refile-by-source")] == [s]
    assert _read(w, "frozen-history") == []
    assert led["header"]["status"] == "complete-with-remaining"


def test_phase_f_d2_the_labels_file_exclusion_holds_when_phase_d_did_not_run(world, tool, capsys):
    w = world
    un = w.cites("lesson", "goal", src_current=0)
    free = w.cites("lesson", "goal", src_current=0)
    labels = {"edges": [lentry(w, *un, [lab("correct-about")])]}
    rc, out, err = apply_(w, tool, capsys, labels=labels, extra=["--phases", "f"])
    assert rc == 0, out + err
    assert w.rels(*un) == ["cites"] and w.rels(*free) == ["about"]
    assert [e["phase"] for e in _f_entry(ledger(w), free)] == ["f"]
    assert not _f_entry(ledger(w), un)


def test_phase_f_e_the_keeper_pair_with_a_superseded_source_is_never_selected_or_exported(
        world, tool, capsys):
    w = world
    keeper = w.cites("observation_factual", "evidence", src_current=0)
    loose = w.cites("lesson", "goal", src_current=0)
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    plan = json.loads(dest.read_text())
    assert not _f_entry(plan, keeper)
    assert {x["source_id"] for x in plan["frozen_history"]} == {loose[0]}
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert w.rels(*keeper) == ["cites"] and w.rels(*loose) == ["about"]
    assert not _f_entry(ledger(w), keeper)
    assert {x["source_id"] for x in _read(w, "frozen-history")} == {loose[0]}
    assert ledger(w)["header"]["loose_cites_remaining"]["total"] == 0


def test_phase_f_f_an_edge_an_earlier_phase_handled_has_exactly_one_ledger_entry(world, tool, capsys):
    w = world
    struct = w.cites("feeling_report", "goal", src_current=0)         # phase b
    bulk = w.cites("definition", "goal", src_current=0)                # phase c
    dup = w.cites("task", "goal", src_current=0)                       # phase a: typed duplicate
    w.edge("derives_from", *dup)
    dup_about = w.cites("lesson", "goal", src_current=0)               # phase a REFUSES (about exists)
    w.edge("about", *dup_about)
    rep = report(strata=[stratum("definition", "goal", {"correct-about": 10})])
    rc, out, err = apply_(w, tool, capsys, rep=rep)
    assert rc == 0, out + err
    led = ledger(w)
    for pair, phase in ((struct, "b"), (bulk, "c"), (dup, "a"), (dup_about, "a")):
        es = _f_entry(led, pair)
        assert [e["phase"] for e in es] == [phase], (pair, es)
    assert _f_entry(led, dup_about)[0]["status"].startswith("refused:")
    assert w.rels(*dup_about) == ["about", "cites"]                    # still loose, never forced
    assert _read(w, "frozen-history") == []
    assert led["header"]["loose_cites_remaining"]["total"] == 1


def test_phase_f_f2_dry_run_a_phase_a_planned_edge_has_one_entry_and_no_phase_f_count(world, tool, capsys):
    """The claimed-by-an-earlier-phase filter matters in a dry run: a phase-a
    PLANNED edge is still a `cites` in the DB, so without the filter act()'s
    first branch would add a spurious (f, skipped-not-cites) entry that
    pollutes the per-phase and per-status counts."""
    w = world
    dup = w.cites("task", "goal", src_current=0)
    w.edge("derives_from", *dup)                                       # phase a PLANS it
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    plan = json.loads(dest.read_text())
    es = _f_entry(plan, dup)
    assert [(e["phase"], e["status"]) for e in es] == [("a", "planned")], es
    h = plan["header"]
    assert h["counts_per_phase"] == {"a": {"planned": 1}}
    assert h["counts_per_status"] == {"planned": 1}
    assert plan["frozen_history"] == []


def test_phase_f_g_the_set_is_listed_by_stratum_and_exported_with_ids_and_statuses(world, tool, capsys):
    w = world
    P = _frozen_world(w)
    out_cur = w.cites("lesson", "goal")                                 # never in the set
    ids = {k: w.eid(*p) for k, p in P.items()}
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    h = ledger(w)["header"]
    assert h["frozen_history_counts"] == {"total": 3, "per_stratum": {
        "lesson->goal": 2, "task->goal": 1}}
    assert "frozen-history" in out and "[lesson->goal] 2" in out and "[task->goal] 1" in out
    d = w.home / "migrations" / "cites-step3"
    (f,) = d.glob("frozen-history-*.json")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    got = {x["edge_id"]: x for x in json.loads(f.read_text())}
    want = {
        ids["sup"]: {"edge_id": ids["sup"], "source_id": P["sup"][0],
                     "target_id": P["sup"][1], "source_status": "superseded",
                     "target_current": True},
        ids["sup_tgt_sup"]: {"edge_id": ids["sup_tgt_sup"],
                             "source_id": P["sup_tgt_sup"][0], "target_id": P["sup_tgt_sup"][1],
                             "source_status": "superseded", "target_current": False},
        ids["retracted"]: {"edge_id": ids["retracted"],
                           "source_id": P["retracted"][0], "target_id": P["retracted"][1],
                           "source_status": "retracted", "target_current": True},
    }
    assert got == want
    assert out_cur[0] not in f.read_text()


def test_phase_f_h_dry_run_plans_f_writes_nothing_and_lists_the_set_the_apply_retypes(world, tool, capsys):
    w = world
    P = _frozen_world(w)
    before = w.dump()
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, extra=["--plan-out", str(dest)])
    assert rc == 0, out + err
    assert w.dump() == before
    plan = json.loads(dest.read_text())
    f_entries = [e for e in plan["entries"] if e["phase"] == "f"]
    assert {(e["source_id"], e["target_id"]) for e in f_entries} == set(P.values())
    assert {(e["to"], e["reason"], e["status"]) for e in f_entries} == {
        ("about", "frozen-history", "planned")}
    assert "phase f: 3 edge(s)" in out and "[lesson->goal] 2" in out
    assert plan["header"]["frozen_history_counts"]["total"] == 3
    assert plan["header"]["loose_cites_remaining"]["total"] == 0          # projected
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    planned = sorted(plan["frozen_history"], key=lambda x: x["edge_id"])
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert sorted(_read(w, "frozen-history"), key=lambda x: x["edge_id"]) == planned
    applied = {e["edge_id"] for e in entries(ledger(w), phase="f", status="applied")}
    assert applied == {x["edge_id"] for x in planned}


def test_phase_f_i_without_f_the_edges_stay_cites_and_are_counted_as_remaining(world, tool, capsys):
    w = world
    P = _frozen_world(w)
    rc, out, err = apply_(w, tool, capsys, extra=["--phases", "a,b,c,d,e"])
    assert rc == 0, out + err
    for p in P.values():
        assert w.rels(*p) == ["cites"]
    h = ledger(w)["header"]
    assert h["loose_cites_remaining"]["total"] == 3
    assert h["loose_cites_remaining"]["non_current_source"] == 3
    assert h["status"] == "complete-with-remaining"
    assert h["phases"] == ["a", "b", "c", "d", "e"]
    assert _read(w, "frozen-history") == []


def test_phase_f_j_a_second_apply_re_applies_no_edge(world, tool, capsys):
    w = world
    P = _frozen_world(w)
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    eh = w.edges_hist()
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 0, out + err
    assert w.edges_hist() == eh
    led = ledger(w)
    assert not entries(led, status="applied")
    assert not entries(led, phase="f") and led["header"]["status"] == "complete"
    for p in P.values():
        assert w.rels(*p) == ["about"]
    # Bounded: "no edge is re-applied", not "no f entries". A second `--phases f`
    # apply retries and records an edge that was refused or deferred, as
    # phases a-e do; those reappear as refused / deferred, never as applied.
    dup_about = w.cites("lesson", "goal", src_current=0)
    w.edge("about", *dup_about)                                        # engine refuses: about exists
    deferred = w.cites("lesson", "goal", src_current=0)
    w.marker(deferred[1])                                              # engine defers: pending marker
    for _ in range(2):
        rc, out, err = apply_(w, tool, capsys, extra=["--phases", "f"])
        assert rc == 0, out + err
        led = ledger(w)
        assert not entries(led, status="applied")
        (r,) = _f_entry(led, dup_about)
        assert r["phase"] == "f" and r["status"].startswith("refused:"), r
        (d,) = _f_entry(led, deferred)
        assert d["phase"] == "f" and d["status"] == "deferred-pending-marker", d
        assert w.rels(*dup_about) == ["about", "cites"] and w.rels(*deferred) == ["cites"]
        for p in P.values():
            assert w.rels(*p) == ["about"]


def test_phase_f_k_the_audit_gate_runs_after_f_and_aborts(world, tool, capsys, monkeypatch):
    w = world
    _frozen_world(w)
    # calls: baseline, after a..e (five, clean), after f (<- a new cycle)
    _fake_audit(monkeypatch, [([], 0)] * 6 + [([CYC2], 1)])
    rc, out, err = apply_(w, tool, capsys)
    assert rc == 3, out + err
    assert "aborted-audit" in out + err and "zz_c" in out + err and "phase f" in out + err
    led = ledger(w)
    assert led["header"]["status"] == "aborted-audit"
    assert len(entries(led, phase="f")) == 3                  # f ran; its retypes stand
    assert set(led["header"]["audit"]["after_phase"]) == {"a", "b", "c", "d", "e", "f"}


def test_phase_f_phases_flag_accepts_f_and_the_default_ends_with_it(world, tool, capsys):
    w = world
    s, t = w.cites("lesson", "goal", src_current=0)
    rc, out, err = invoke(w, tool, capsys, extra=["--phases", "f"])
    assert rc == 0, out + err
    assert "phase f: 1 edge(s)" in out and "phase a" not in out
    assert w.rels(s, t) == ["cites"]                          # dry run
    assert tool.PHASES == ("a", "b", "c", "d", "e", "f")
    assert "six phases" in tool.__doc__


# ---------------------------------------------------------------------------
# Kepler's ruling: a derivation whose reasoning CLASS is inductive_corroboration
# (the engine's REASONING_CLASS: inductive_generalization, inductive_enumeration,
# authority_consensus) is HAND-ONLY. `inductive_corroboration` is that class
# name, not a reasoning type. A premise wired into a generalization
# must be one of its cases; a precision sentence, a recommendation or a
# restatement goes to `about`, so the tool must never list such a source as a
# plain retract+refile (measured on one seat: wiring such a node as a premise
# inflated a derivation's confidence 0.777 -> 0.941). The tool never refiles:
# it lists. The contract is on the LIST, and on `--apply` writing nothing but
# retypes. (contract-first: these cells were written before the implementation)
#
#   H1  the reasoning type is read from nodes.metadata with the guarded SQL form
#   H2  generalization / corroboration -> action "hand-only" (+ reasoning_type,
#       + why); every other READABLE type keeps "retract+refile" unchanged
#   H3  fail closed: missing / empty / unreadable / malformed -> hand-only with
#       why "reasoning-type-unreadable", and a malformed blob never aborts a run
#   H4  every emission site agrees (per-edge needs entry, refile export, printed
#       summary, needs-seat-read file, ledger header, docstring)
#   H5  --apply invokes no engine verb but the retype, and never on the planted edge
# ---------------------------------------------------------------------------

REFILE_TYPES = ("deductive_modus_ponens", "abductive_best_explanation", "inductive_analogy",
                "inductive_causal", "abductive_elimination", "deductive_hypothetical_syllogism")
PRECISION = ("Precision: the figure above holds only for the sampled seats; "
             "recommendation: read it as a bound, not a case.")
UNREADABLE = "reasoning-type-unreadable"
UNRECOGNIZED = "reasoning-type-unrecognized"


def _force_metadata(w, node_id, raw):
    """Store `raw` verbatim in nodes.metadata, bypassing the write-guard
    triggers (a real graph can still hold a bad blob written before them)."""
    c = w.conn()
    try:
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                 "AND name LIKE 'nodes_metadata_json_guard%'").fetchall():
            c.execute(f'DROP TRIGGER "{name}"')
        c.execute("UPDATE nodes SET metadata=? WHERE id=?", (raw, node_id))
        c.commit()
    finally:
        c.close()


def _mf_label_for(reason="quoted: 'because of X'"):
    return [lab("mis-filed-premise", "derives_from", premise="relies", reason=reason)]


def _planted(w, meta=_DEFAULT_META, ttype="observation_factual", tclaim=PRECISION):
    """A derivation source (metadata as given) with a labelled mis-filed-premise
    cites edge to a precision-sentence node. Returns (source, target, labels)."""
    src = w.node("derivation", created=NEW, metadata=meta)
    t = w.node(ttype, created=OLD, claim=tclaim)
    w.edge("cites", src, t)
    return src, t, {"edges": [lentry(w, src, t, _mf_label_for())]}


def _plan(w, tool, capsys, labels, apply=False):
    if apply:
        rc, out, err = apply_(w, tool, capsys, labels=labels)
        d = w.home / "migrations" / "cites-step3"
        return rc, out, err, {
            "refile_by_source": json.loads(next(d.glob("refile-by-source-*.json")).read_text()),
            "needs_seat_read": json.loads(next(d.glob("needs-seat-read-*.json")).read_text()),
            "entries": ledger(w)["entries"], "header": ledger(w)["header"]}
    dest = w.inputs / "plan.json"
    rc, out, err = invoke(w, tool, capsys, rep=report(), labels=labels, extra=["--plan-out", str(dest)])
    return rc, out, err, json.loads(dest.read_text())


def _only(doc, src):
    (g,) = [g for g in doc["refile_by_source"] if g["source_id"] == src]
    return g


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_hand_only_a_precision_sentence_on_a_generalization_is_listed_hand_only_not_refiled(
        world, tool, capsys, apply):
    w = world
    src, t, labels = _planted(w, meta=rt("inductive_generalization"))
    snap = w.dump() if not apply else None
    rc, out, err, doc = _plan(w, tool, capsys, labels, apply=apply)
    assert rc == 0, out + err
    if snap is not None:
        assert w.dump() == snap
    g = _only(doc, src)
    assert g["action"] == "hand-only" and g["reasoning_type"] == "inductive_generalization"
    assert g["why"] and UNREADABLE not in g["why"]
    assert [x["target_id"] for x in g["targets"]] == [t]
    # nothing was refiled or retyped: the edge is still the very same cites edge
    assert w.rels(src, t) == ["cites"] and w.retype_reasons() == []
    # the per-edge needs entry and the needs-seat-read export agree with the list
    (e,) = [e for e in doc["entries"] if (e["source_id"], e["target_id"]) == (src, t)]
    assert e["status"] == "needs-seat-read" and e["to"] is None and e["action"] == "hand-only"
    (n,) = [n for n in doc["needs_seat_read"] if (n["source_id"], n["target_id"]) == (src, t)]
    assert n["action"] == "hand-only"
    assert "retract+refile" not in json.dumps(doc["refile_by_source"] + doc["needs_seat_read"])


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_hand_only_control_same_fixture_on_modus_ponens_stays_retract_refile(world, tool, capsys, apply):
    w = world
    src, t, labels = _planted(w, meta=rt("deductive_modus_ponens"))
    rc, out, err, doc = _plan(w, tool, capsys, labels, apply=apply)
    assert rc == 0, out + err
    g = _only(doc, src)
    assert g["action"] == "retract+refile"          # unchanged by the hand-only rule
    (e,) = [e for e in doc["entries"] if (e["source_id"], e["target_id"]) == (src, t)]
    assert e["action"] == "retract+refile"
    assert w.rels(src, t) == ["cites"]


def test_hand_only_a_refile_entry_carries_its_reasoning_type_and_no_why(world, tool, capsys):
    w = world
    src, t, labels = _planted(w, meta=rt("deductive_modus_ponens"))
    rc, out, err, doc = _plan(w, tool, capsys, labels)
    g = _only(doc, src)
    assert g["reasoning_type"] == "deductive_modus_ponens" and "why" not in g


def test_hand_only_inductive_corroboration_is_hand_only(world, tool, capsys):
    """The corroboration CLASS is hand-only through its real member types
    (inductive_enumeration here); the class name is not a type."""
    w = world
    src, t, labels = _planted(w, meta=rt("inductive_enumeration"))
    rc, out, err, doc = _plan(w, tool, capsys, labels)
    assert rc == 0, out + err
    g = _only(doc, src)
    assert g["action"] == "hand-only" and g["reasoning_type"] == "inductive_enumeration"
    assert g["why"] and g["why"] not in (UNREADABLE, UNRECOGNIZED)


@pytest.mark.parametrize("rtype", REFILE_TYPES)
def test_hand_only_every_other_readable_type_stays_retract_refile(world, tool, capsys, rtype):
    w = world
    src, t, labels = _planted(w, meta=rt(rtype))
    rc, out, err, doc = _plan(w, tool, capsys, labels)
    assert rc == 0, out + err
    g = _only(doc, src)
    assert g["action"] == "retract+refile"


@pytest.mark.parametrize("case", ["no_key", "null", "not_json", "empty_string", "json_array",
                                  "json_scalar", "empty_type", "null_type", "number_type",
                                  "whitespace_type"])
def test_hand_only_fail_closed_when_the_reasoning_type_is_unreadable(world, tool, capsys, case):
    w = world
    src, t, labels = _planted(w, meta=None)
    raw = {"no_key": json.dumps({"confidence_note": "x"}), "null": None, "not_json": "{not json",
           "empty_string": "", "json_array": "[1, 2]", "json_scalar": "5",
           "empty_type": json.dumps({"reasoning_type": ""}),
           "null_type": json.dumps({"reasoning_type": None}),
           "number_type": json.dumps({"reasoning_type": 7}),
           "whitespace_type": json.dumps({"reasoning_type": "  "})}[case]
    _force_metadata(w, src, raw)
    snap = w.dump()
    rc, out, err, doc = _plan(w, tool, capsys, labels)
    assert rc == 0, out + err                      # a bad blob never aborts the run
    assert w.dump() == snap
    g = _only(doc, src)
    assert g["action"] == "hand-only" and g["why"] == UNREADABLE and g["reasoning_type"] is None
    (e,) = [e for e in doc["entries"] if (e["source_id"], e["target_id"]) == (src, t)]
    assert e["action"] == "hand-only"
    assert w.rels(src, t) == ["cites"]


def test_hand_only_a_malformed_blob_on_one_source_does_not_abort_the_other_sources(world, tool, capsys):
    w = world
    bad, t1, l1 = _planted(w, meta=None)
    _force_metadata(w, bad, "{not json")
    good, t2, l2 = _planted(w, meta=rt("deductive_modus_ponens"))
    rc, out, err, doc = _plan(w, tool, capsys, {"edges": l1["edges"] + l2["edges"]}, apply=True)
    assert rc == 0, out + err
    assert _only(doc, bad)["action"] == "hand-only"
    assert _only(doc, good)["action"] == "retract+refile"


def test_hand_only_the_reader_treats_a_missing_source_row_as_unreadable(world, tool):
    """The reader itself: no row, no type -> unreadable (never the safe case)."""
    w = world
    conn = w.conn()
    try:
        assert tool.read_reasoning_type(conn, "zz_does_not_exist") is None
        src = w.node("derivation", metadata=rt("inductive_generalization"))
        assert tool.read_reasoning_type(conn, src) == "inductive_generalization"
    finally:
        conn.close()


def test_hand_only_a_superseded_source_is_still_read_by_id(world, tool, capsys):
    """Currentness and status do not hide the type: a closed derivation's
    metadata is read like any other."""
    w = world
    src = w.node("derivation", current=0, created=NEW, metadata=rt("inductive_generalization"))
    t = w.node("observation_factual", created=OLD, claim=PRECISION)
    w.edge("cites", src, t)
    rc, out, err, doc = _plan(w, tool, capsys, {"edges": [lentry(w, src, t, _mf_label_for())]})
    assert rc == 0, out + err
    g = _only(doc, src)
    assert g["action"] == "hand-only" and g["reasoning_type"] == "inductive_generalization"


def test_hand_only_two_targets_of_one_generalization_make_one_entry(world, tool, capsys):
    w = world
    src = w.node("derivation", created=NEW, metadata=rt("inductive_generalization"))
    t1, t2 = _mf(w, src), _mf(w, src)
    labels = {"edges": [lentry(w, src, t, _mf_label_for(f"q{i}")) for i, t in enumerate((t1, t2))]}
    rc, out, err, doc = _plan(w, tool, capsys, labels, apply=True)
    assert rc == 0, out + err
    assert len(doc["refile_by_source"]) == 1
    g = _only(doc, src)
    assert g["action"] == "hand-only" and sorted(x["target_id"] for x in g["targets"]) == sorted([t1, t2])
    assert doc["header"]["refile_counts"] == {"edges": 2, "sources": 1}
    assert doc["header"]["hand_only_counts"] == {"edges": 2, "sources": 1}


def test_hand_only_mixed_sources_interleaved_each_gets_its_own_verdict(world, tool, capsys):
    """Edge ids interleave (B, A, B, C, A, D, B): A a generalization, B modus
    ponens, C an authority_consensus (corroboration class), D unreadable. The verdict is per SOURCE, never
    taken from whichever candidate came first."""
    w = world
    a = w.node("derivation", created=NEW, metadata=rt("inductive_generalization"))
    b = w.node("derivation", created=NEW, metadata=rt("deductive_modus_ponens"))
    c = w.node("derivation", created=NEW, metadata=rt("authority_consensus"))
    d = w.node("derivation", created=NEW, metadata=None)
    _force_metadata(w, d, "{not json")
    pairs = [(b, _mf(w, b)), (a, _mf(w, a)), (b, _mf(w, b)), (c, _mf(w, c)),
             (a, _mf(w, a)), (d, _mf(w, d)), (b, _mf(w, b))]
    labels = {"edges": [lentry(w, s, t, _mf_label_for(f"q{i}")) for i, (s, t) in enumerate(pairs)]}
    rc, out, err, doc = _plan(w, tool, capsys, labels, apply=True)
    assert rc == 0, out + err
    got = {g["source_id"]: (g["action"], len(g["targets"])) for g in doc["refile_by_source"]}
    assert got == {a: ("hand-only", 2), b: ("retract+refile", 3), c: ("hand-only", 1),
                   d: ("hand-only", 1)}
    assert _only(doc, d)["why"] == UNREADABLE
    acts = {e["source_id"]: e["action"] for e in doc["entries"] if e["status"] == "needs-seat-read"}
    assert acts == {a: "hand-only", b: "retract+refile", c: "hand-only", d: "hand-only"}
    assert doc["header"]["refile_counts"] == {"edges": 7, "sources": 4}
    assert doc["header"]["hand_only_counts"] == {"edges": 4, "sources": 3}


def test_hand_only_printed_summary_counts_hand_only_separately(world, tool, capsys):
    w = world
    a, ta, la = _planted(w, meta=rt("inductive_generalization"))
    b, tb, lb = _planted(w, meta=rt("deductive_modus_ponens"))
    rc, out, err, doc = _plan(w, tool, capsys, {"edges": la["edges"] + lb["edges"]})
    assert rc == 0, out + err
    assert ("refile list: 2 source(s) (retract+refile: 1, hand-only: 1); 2 edge(s); "
            "hand-only sources are never refiled, the premise role is read from the chain") in out
    assert "retract+refile list:" not in out
    line_a = next(l for l in out.splitlines() if l.strip().startswith(f"source {a} "))
    line_b = next(l for l in out.splitlines() if l.strip().startswith(f"source {b} "))
    assert "HAND-ONLY" in line_a and "inductive_generalization" in line_a
    assert "HAND-ONLY" not in line_b
    assert "needs-seat-read [mis-filed-premise] action=hand-only" in out
    assert "needs-seat-read [mis-filed-premise] action=retract+refile" in out


def test_hand_only_ledger_header_carries_the_counts_and_the_entry_the_type(world, tool, capsys):
    w = world
    src, t, labels = _planted(w, meta=rt("inductive_generalization"))
    rc, out, err, doc = _plan(w, tool, capsys, labels, apply=True)
    assert rc == 0, out + err
    assert doc["header"]["hand_only_counts"] == {"edges": 1, "sources": 1}
    assert doc["header"]["refile_counts"] == {"edges": 1, "sources": 1}
    (e,) = [e for e in doc["entries"] if (e["source_id"], e["target_id"]) == (src, t)]
    assert e["reasoning_type"] == "inductive_generalization"


def test_hand_only_module_docstring_states_the_rule_and_the_reason(tool):
    doc = " ".join(tool.__doc__.split())
    assert "hand-only" in doc and "inductive_generalization" in doc and "inductive_corroboration" in doc
    assert "reasoning-type-unreadable" in doc
    assert "one of its cases" in doc


def test_hand_only_reads_the_type_with_the_guarded_json_extract_only():
    src = TOOL_PATH.read_text()
    # the value through the guarded json_extract; duplicate keys counted through
    # a guarded json_each (json_extract alone would return the first duplicate)
    assert "json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.reasoning_type')" in src
    assert "FROM json_each(CASE WHEN json_valid(metadata) THEN metadata END)" in src
    assert not re.search(r"json_extract\(\s*(?:n\.)?metadata\s*,", src)


import engram_confidence as _ec

CORROBORATION_CLASS = "inductive_corroboration"
CORROBORATION_TYPES = tuple(sorted(t for t in _ec.REASONING_TYPES
                                   if _ec.REASONING_CLASS[t] == CORROBORATION_CLASS))
OTHER_ENGINE_TYPES = tuple(sorted(t for t in _ec.REASONING_TYPES
                                  if _ec.REASONING_CLASS[t] != CORROBORATION_CLASS))
ENGINE_SRC = REPO / "src" / "engram"


def _plan_type(w, tool, capsys, meta):
    (w.inputs / "plan.json").unlink(missing_ok=True)      # a cell may plan twice
    src, t, labels = _planted(w, meta=meta)
    rc, out, err, doc = _plan(w, tool, capsys, labels)
    assert rc == 0, out + err
    return src, t, _only(doc, src), doc


# F1 / F7: the rule is keyed on the engine's reasoning CLASS, from its own constants

def test_hand_only_the_engine_class_holds_the_three_types_the_ruling_names():
    """Pins the premise of the fold: the class has these members today (the
    cells below are parametrized over the engine, so they track a change)."""
    assert CORROBORATION_TYPES == ("authority_consensus", "inductive_enumeration",
                                   "inductive_generalization")
    assert CORROBORATION_CLASS not in _ec.REASONING_TYPES      # a class name, not a type


@pytest.mark.parametrize("rtype", CORROBORATION_TYPES)
def test_hand_only_every_corroboration_class_type_is_hand_only(world, tool, capsys, rtype):
    src, t, g, doc = _plan_type(world, tool, capsys, rt(rtype))
    assert g["action"] == "hand-only" and g["reasoning_type"] == rtype
    assert g["why"] == tool.HAND_ONLY_WHY
    assert world.rels(src, t) == ["cites"]


@pytest.mark.parametrize("rtype", OTHER_ENGINE_TYPES)
def test_hand_only_every_other_engine_type_stays_retract_refile(world, tool, capsys, rtype):
    """Control: same fixture, every real type of another class (incl.
    inductive_statistical, inductive_analogy, authority_expert) stays refile."""
    src, t, g, doc = _plan_type(world, tool, capsys, rt(rtype))
    assert g["action"] == "retract+refile" and g["reasoning_type"] == rtype and "why" not in g


def test_hand_only_class_level_rule_covers_exactly_the_engine_class_members(world, tool):
    w = world
    conn = w.conn()
    try:
        for rtype in sorted(_ec.REASONING_TYPES):
            n = w.node("derivation", metadata=rt(rtype))
            got = tool.classify_derivation(conn, n)
            want_hand = _ec.REASONING_CLASS[rtype] == CORROBORATION_CLASS
            assert got[1] is want_hand, (rtype, got)
            assert got[0] == rtype and (got[2] is not None) == want_hand
    finally:
        conn.close()


def test_hand_only_a_type_added_to_the_class_is_covered_without_editing_the_tool(
        world, tool, capsys, monkeypatch):
    """A hand-copied type list would stay green on the real types and miss this."""
    monkeypatch.setattr(_ec, "REASONING_TYPES", _ec.REASONING_TYPES | {"inductive_future_kind"})
    monkeypatch.setitem(_ec.REASONING_CLASS, "inductive_future_kind", CORROBORATION_CLASS)
    src, t, g, doc = _plan_type(world, tool, capsys, rt("inductive_future_kind"))
    assert g["action"] == "hand-only" and g["why"] == tool.HAND_ONLY_WHY
    # control: the same new type filed under a different class is a plain refile
    monkeypatch.setitem(_ec.REASONING_CLASS, "inductive_future_kind", "deductive")
    src2, t2, g2, doc2 = _plan_type(world, tool, capsys, rt("inductive_future_kind"))
    assert g2["action"] == "retract+refile"


# F2: an unrecognized (well-formed) type name fails closed

@pytest.mark.parametrize("name", ["inductive_generalisation", "inductive_corroboration",
                                  "inductve_generalization", "some_future_type"])
def test_hand_only_an_unrecognized_type_name_fails_closed(world, tool, capsys, name):
    assert name not in _ec.REASONING_TYPES
    src, t, g, doc = _plan_type(world, tool, capsys, rt(name))
    assert g["action"] == "hand-only" and g["why"] == UNRECOGNIZED
    assert g["reasoning_type"] == name
    (e,) = [e for e in doc["entries"] if (e["source_id"], e["target_id"]) == (src, t)]
    assert e["action"] == "hand-only" and e["reasoning_type"] == name
    assert world.rels(src, t) == ["cites"]


def test_hand_only_inductive_statistical_is_a_real_type_and_stays_retract_refile(world, tool, capsys):
    assert "inductive_statistical" in _ec.REASONING_TYPES
    src, t, g, doc = _plan_type(world, tool, capsys, rt("inductive_statistical"))
    assert g["action"] == "retract+refile"


# F3: a duplicate reasoning_type key is unreadable (json_extract = first, json.loads = last)

DUP_HAND_FIRST = '{"reasoning_type": "inductive_generalization", "reasoning_type": "deductive_modus_ponens"}'
DUP_SAFE_FIRST = '{"reasoning_type": "deductive_modus_ponens", "reasoning_type": "inductive_generalization"}'
DUP_BOTH_SAFE = '{"reasoning_type": "deductive_modus_ponens", "reasoning_type": "abductive_elimination"}'


@pytest.mark.parametrize("raw", [DUP_HAND_FIRST, DUP_SAFE_FIRST, DUP_BOTH_SAFE],
                         ids=["hand-only-first", "safe-first", "both-safe"])
def test_hand_only_a_duplicate_reasoning_type_key_is_unreadable(world, tool, capsys, raw):
    w = world
    src, t, labels = _planted(w, meta=None)
    _force_metadata(w, src, raw)
    rc, out, err, doc = _plan(w, tool, capsys, labels)
    assert rc == 0, out + err
    g = _only(doc, src)
    assert g["action"] == "hand-only" and g["why"] == UNREADABLE and g["reasoning_type"] is None


def test_hand_only_a_single_key_control_beside_the_duplicate_cells_is_read(world, tool):
    """Control: the same object with ONE reasoning_type key reads normally, and a
    duplicate of an unrelated key does not make the type unreadable."""
    w = world
    a = w.node("derivation", metadata='{"reasoning_type": "deductive_modus_ponens"}')
    b = w.node("derivation",
               metadata='{"reasoning_type": "deductive_modus_ponens", "note": 1, "note": 2}')
    conn = w.conn()
    try:
        assert tool.read_reasoning_type(conn, a) == "deductive_modus_ponens"
        assert tool.read_reasoning_type(conn, b) == "deductive_modus_ponens"
    finally:
        conn.close()


# F6: the normalisation is pinned

def test_hand_only_the_type_is_stripped_and_lower_cased_before_the_class_lookup(world, tool, capsys):
    src, t, g, doc = _plan_type(world, tool, capsys, rt("  Inductive_Enumeration \t"))
    assert g["action"] == "hand-only" and g["reasoning_type"] == "inductive_enumeration"
    assert g["why"] == tool.HAND_ONLY_WHY
    # control: a padded, mixed-case SAFE type is normalised too and stays refile
    src2, t2, g2, doc2 = _plan_type(world, tool, capsys, rt(" Deductive_Modus_Ponens "))
    assert g2["action"] == "retract+refile" and g2["reasoning_type"] == "deductive_modus_ponens"


# F5: the printed label is honest

def test_hand_only_summary_label_counts_sources_and_splits_them(world, tool, capsys):
    w = world
    a, ta, la = _planted(w, meta=rt("inductive_enumeration"))
    b, tb, lb = _planted(w, meta=rt("authority_consensus"))
    c, tc, lc = _planted(w, meta=rt("deductive_modus_ponens"))
    rc, out, err, doc = _plan(w, tool, capsys, {"edges": la["edges"] + lb["edges"] + lc["edges"]})
    assert rc == 0, out + err
    assert "refile list: 3 source(s) (retract+refile: 1, hand-only: 2); 3 edge(s)" in out
    assert "retract+refile list:" not in out


def test_hand_only_summary_label_with_no_hand_only_source_reads_zero(world, tool, capsys):
    w = world
    a, ta, la = _planted(w, meta=rt("deductive_modus_ponens"))
    rc, out, err, doc = _plan(w, tool, capsys, la)
    assert rc == 0, out + err
    assert "refile list: 1 source(s) (retract+refile: 1, hand-only: 0); 1 edge(s)" in out


# F4: the spy enumerates every `*_impl`, and the planted rows are compared before/after

def _engine_modules():
    """Every module under src/engram (tools/ excluded) that the engine load and
    the server the world builds left in sys.modules."""
    import importlib
    for name in ("engram_core", "engram_cornerstone", "engram_dag_conformance",
                 "engram_revision", "engram_derive", "server"):
        importlib.import_module(name)
    mods = []
    for name, mod in sorted(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        p = Path(f).resolve()
        if ENGINE_SRC in p.parents and p.relative_to(ENGINE_SRC).parts[0] != "tools":
            mods.append(mod)
    return mods


def _spy_every_engine_impl(monkeypatch):
    """Wrap EVERY `*_impl` function bound on every engine module. A call made
    while another spied call is running (the retype's own internals) is not
    recorded, so the one verb the tool may use does not trip its own spy; every
    top-level call is. Returns (calls, names)."""
    calls, names, depth = [], set(), [0]
    for mod in _engine_modules():
        for attr in sorted(vars(mod)):
            real = vars(mod)[attr]
            if not (attr.endswith("_impl") and callable(real)):
                continue
            names.add(attr)

            def spy(*a, _n=attr, _real=real, **kw):
                if depth[0] == 0:
                    calls.append((_n, a, kw))
                depth[0] += 1
                try:
                    return _real(*a, **kw)
                finally:
                    depth[0] -= 1

            monkeypatch.setattr(mod, attr, spy)
    return calls, names


def _rows(w, src, t):
    c = w.conn()
    try:
        return {
            "source": [tuple(r) for r in c.execute("SELECT * FROM nodes WHERE id=?", (src,))],
            "target": [tuple(r) for r in c.execute("SELECT * FROM nodes WHERE id=?", (t,))],
            "edges": [tuple(r) for r in c.execute(
                "SELECT * FROM edges WHERE source_id=? OR target_id=? ORDER BY id", (src, src))],
            "history": [tuple(r) for r in c.execute(
                "SELECT * FROM edit_history WHERE node_id IN (?, ?) ORDER BY rowid", (src, t))],
        }
    finally:
        c.close()


def test_hand_only_the_spy_wraps_every_impl_on_every_loaded_engine_module(world, tool, monkeypatch):
    calls, names = _spy_every_engine_impl(monkeypatch)
    # not a hand-written list: it holds the old ten, the cornerstone verbs, and more
    assert {"_retype_edge_impl", "_derive_impl", "_supersede_impl", "_retract_impl",
            "_outgrow_cornerstone_impl"} <= names
    assert len(names) > 10
    import engram_cornerstone
    assert engram_cornerstone._outgrow_cornerstone_impl.__name__ == "spy"
    # a verb outside the old ten is recorded when called at top level
    engram_cornerstone._outgrow_cornerstone_impl(old_cornerstone_id="zz_does_not_exist")
    assert [c[0] for c in calls] == ["_outgrow_cornerstone_impl"]


def test_hand_only_apply_calls_no_engine_verb_but_retype_and_never_on_the_planted_edge(
        world, tool, capsys, monkeypatch):
    w = world
    plant_phases(w)                                       # ordinary retypable edges, phases a-c
    src, t, mf = _planted(w, meta=rt("inductive_generalization"))
    s2, t2 = w.cites("lesson", "goal")                    # an ordinary labelled retype (phase d)
    labels = {"edges": mf["edges"] + [lentry(w, s2, t2, [lab("correct-about")])]}
    rep = plant_report()
    rep["strata"].append(stratum("derivation", "observation_factual", {"correct-about": 10}))
    planted_eid = w.eid(src, t)
    before = _rows(w, src, t)
    assert before["source"] and before["edges"]            # the rows exist to be compared
    calls, names = _spy_every_engine_impl(monkeypatch)
    rc, out, err = apply_(w, tool, capsys, rep=rep, labels=labels,
                          hand=handcheck(edges_read=list(range(1, 11)), agree=10))
    assert rc == 0, out + err
    assert calls, "the spy saw no call at all: the cell would prove nothing"
    assert {c[0] for c in calls} == {"_retype_edge_impl"}, sorted({c[0] for c in calls})
    retyped = {(c[2]["source_id"], c[2]["target_id"]) for c in calls}
    assert (src, t) not in retyped and (s2, t2) in retyped
    assert all(c[2]["source_id"] != src and c[2]["target_id"] != t for c in calls)
    # the planted edge is exactly as it was: same id, still cites, no history on its pair
    assert w.edge_map()[planted_eid] == (src, t, "cites")
    assert w.rels(src, t) == ["cites"]
    assert all(n not in (src, t) for n, _ in w.retype_reasons())
    # every stored row about the planted source, target and edge is identical, so a
    # direct UPDATE that bypasses every engine verb is caught too
    assert _rows(w, src, t) == before
    by = by_edge(ledger(w))
    assert by[(src, t)]["status"] == "needs-seat-read" and by[(src, t)]["action"] == "hand-only"
