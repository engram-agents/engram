"""Contract-first tests: one final total-budget pass in the lesson-tripwire hook
(#3677, extends the whole-unit-cut pattern of #3671 / #3674).

Before: main() joined [tripwire block (bounded), chain block, in-turn recall]
with NO bound on the total, and the harness silently cuts the TAIL of an
oversized injection, where the chain and the recall sit.

Contract (budget = get_harness_budgets(detect_harness())["budget"]):
  C1  len(additionalContext) <= budget, always.
  C2  byte-identical to the pre-change hook when everything fits.
  C3  cut order: recall (whole id lines from the end), then the chain (one
      whole block), never the tripwire block; never mid-line.
  C4  the recall cut happens inside check_in_turn_recall, before any state is
      written: recent_ids / route ledger / shown record / fire ledger list only
      survivors; cut ids carry the cut name "tail_budget"; all cut => outcome
      "budget_cut" and "".
  C5  the FIRST line says so: " · tail cut: R recall lines, C chain lines"
      inside the trailer (or its own first line when there is no block);
      counted inside the budget via _TAIL_CUT_RESERVE.
  C6  the A/B arm draw is untouched.
  C7  the #3674 _fits guard stays.
  C9  WS4 row: chain_chars unchanged (would-render), chain_cut added,
      chain_rendered False when the chain was cut.
  C10 fail-open: an exception inside the pass leaves dev-shaped output, rc 0.

Every cell runs the hook as a SUBPROCESS (10 s timeout) from a FLATTENED copy of
the hook directory with CLAUDE_PLUGIN_ROOT unset, through a small driver that
patches the loaders/daemon (the hook's own main() runs for real).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_SRC_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK_NAME = "engram-lesson-tripwire-hook.py"
DEV_BASE = "4aa79f31"  # origin/dev the change was cut from

SESSION = "sess-3677"
TERMS = ("alphaterm", "betaterm")
COMMAND = "tripwirepat alphaterm betaterm"
FOOTER = "  (ambient action-moment recall; engram_inspect(id) for detail)"
TRAILER_RE = re.compile(r"^▸ (\d+) shown · (\d+) cut")
CLAUSE_RE = re.compile(r" · tail cut: (\d+) recall lines, (\d+) chain lines")
OWN_LINE_RE = re.compile(r"^▸ tail cut: (\d+) recall lines, (\d+) chain lines$")

_spec = importlib.util.spec_from_file_location("_hook_consts_3677", HOOK_SRC_DIR / HOOK_NAME)
sys.path.insert(0, str(HOOK_SRC_DIR))
_HOOK_CONSTS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_HOOK_CONSTS)
HEADER = _HOOK_CONSTS.TRIPWIRE_HEADER
TRAILER0 = "▸ 1 shown · 0 cut"
BLOCK_OVERHEAD = len(TRAILER0) + 1 + len(HEADER) + 1 + len("  • ")

DRIVER = r'''
import importlib.util, io, json, os, sqlite3, sys, types
hook_path, scen_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
scen = json.load(open(scen_path))
home = os.environ["ENGRAM_HOME"]
os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
cfg = {"auto_surface": {"in_turn_recall": {"enabled": True, "max_lines": scen.get("max_lines", 3)}}}
if scen.get("ws4"):
    cfg["experiments"] = {"ws4_chain_ab": {"enabled": True, "seed": scen["ws4"],
        "start": "2000-01-01T00:00:00Z", "end": "2999-01-01T00:00:00Z"}}
json.dump(cfg, open(os.path.join(home, "config.json"), "w"))
conn = sqlite3.connect(os.path.join(home, "knowledge.db"))
conn.execute("CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, is_current INT, memory_status TEXT, metadata TEXT)")
conn.execute("INSERT OR REPLACE INTO nodes VALUES ('seed_1','observation','s',1,'active','{}')")
conn.commit(); conn.close()
fake = types.ModuleType("engram_idf")
terms = list(scen["terms"])
fake.extract_keywords = lambda conn, text, min_idf=4.0, top_k=5, stopwords=None, n_docs=None: [(t, 7.5) for t in terms]
fake.STOPWORDS = frozenset()
fake.JUNK_STOPLIST = frozenset()
sys.modules["engram_idf"] = fake
if scen.get("probe"):
    os.makedirs(os.path.join(home, "probe", "mods-recall", scen["session"]), exist_ok=True)
spec = importlib.util.spec_from_file_location("tripwire_under_test", hook_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
tw = [tuple(t) for t in scen["tripwires"]]
mod.load_tripwires = lambda *a, **k: tw
mod.load_prevention_lessons = lambda *a, **k: []
chain = scen.get("chain") or ""
mod.render_triggered_principles = lambda hits, heads_out=None: chain
resp = scen.get("response")
def _daemon(q, top_k=5):
    if scen.get("block_recall"):
        import time
        open(scen["recall_marker"], "w").write("reached")  # tells the test the hook got here
        time.sleep(60)  # a recall daemon that never answers (the test kills the hook)
    return resp
mod._query_recall_daemon = _daemon
mod.detect_harness = lambda: scen.get("harness")
if scen.get("raise_in"):
    getattr(mod, scen["raise_in"])  # AttributeError when the name does not exist
    def _boom(*a, **k):
        raise RuntimeError("injected pass failure")
    setattr(mod, scen["raise_in"], _boom)
hook_input = {"session_id": scen["session"], "transcript_path": "", "tool_name": "Bash",
              "tool_input": {"command": scen["command"]}}
if scen.get("tool_use_id"):
    hook_input["tool_use_id"] = scen["tool_use_id"]
sys.stdin = io.StringIO(json.dumps(hook_input))
buf = io.StringIO()
real_out = sys.stdout
sys.stdout = buf
rc = 0
try:
    mod.main()
except SystemExit as e:
    rc = e.code or 0
sys.stdout = real_out
json.dump({"stdout": buf.getvalue(), "rc": rc}, open(out_path, "w"))
'''


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def flat(tmp_path_factory):
    """new = this tree's hook dir flattened; dev = same plus the base commit's hook."""
    root = tmp_path_factory.mktemp("flat3677")
    new = root / "new"
    new.mkdir()
    for p in HOOK_SRC_DIR.glob("*.py"):
        shutil.copy(p, new / p.name)
    # the deployed layout ships the flag helper at the plugin root (#3665)
    shutil.copy(PROJECT_ROOT / "src" / "engram" / "engramd_owner_flags.py", root / "engramd_owner_flags.py")
    dev = root / "dev"
    shutil.copytree(new, dev)
    try:
        src = subprocess.run(
            ["git", "show", f"{DEV_BASE}:src/engram/hooks/claude/{HOOK_NAME}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except Exception:  # noqa: BLE001
        src = None
    if src is not None:
        (dev / HOOK_NAME).write_text(src)
    drv = root / "driver"
    drv.mkdir()
    (drv / "driver.py").write_text(DRIVER)
    return {"new": new / HOOK_NAME, "dev": (dev / HOOK_NAME) if src is not None else None,
            "driver": drv / "driver.py", "root": root}


def _ids(*nums, prefix="ph"):
    return [f"{prefix}_{n}" for n in nums]


def nudge_for_block(total_len: int, tag: str = "NUDGE") -> str:
    n = total_len - BLOCK_OVERHEAD
    assert n > len(tag) + 1
    return tag + "-" + "x" * (n - len(tag) - 1)


def recall_line(nid: str, total_len: int) -> str:
    base = f"  - [{nid}] `kw` — "
    return base + "y" * (total_len - len(base))


def recall_text(lines, terms=TERMS) -> str:
    return (f"[ENGRAM in-turn recall — novel terms: {', '.join(terms)}]\n"
            + "\n".join(lines) + "\n" + FOOTER)


def chain_block(n_lines: int, line_len: int) -> str:
    return "\n".join(["[triggered principles]"] + ["  · " + "c" * line_len] * n_lines)


def response_for(ids, line_len):
    """daemon response whose rendered lines are exactly line_len chars each."""
    top = []
    for nid in ids:
        base = len(f"  - [{nid}] `kw` — ")
        top.append({"id": nid, "type": "observation", "created_at": "2026-10-01T00:00:00+00:00",
                    "recall_summary": "y" * (line_len - base), "recall_keywords": ["kw"]})
    return {"match_count": len(ids), "special_nodes": [], "top_claims": top, "matched_ids": list(ids)}


def expected_keep(lines, limit):
    """Independent re-derivation: largest k with the REAL recall text <= limit."""
    for k in range(len(lines), 0, -1):
        if len(recall_text(lines[:k])) <= limit:
            return k
    return 0


class Run:
    def __init__(self, out, home, ctx, rc):
        self.out, self.home, self.rc = out, home, rc
        self.ctx = ctx

    def read_jsonl(self, rel):
        p = self.home / rel
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []

    def state(self):
        p = self.home / "in-turn-recall-state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def shown(self):
        return self.read_jsonl(f"sessions/{SESSION}.shown.jsonl")

    def routes(self):
        p = self.home / "probe" / "mods-recall" / SESSION / "routes" / "tool_recall.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def fires(self):
        return self.read_jsonl("in-turn-recall-ledger.jsonl")

    def ws4(self):
        return self.read_jsonl("tripwire-fire-ledger.jsonl")


def run(flat, tmp_path, *, which="new", harness="claude-code", nudges=None, chain=None,
        response=None, terms=TERMS, ws4=None, tool_use_id=None, raise_in=None, max_lines=3,
        command=COMMAND, sub="r", block_recall=False, kill_after=None):
    base = tmp_path / sub
    home = base / "engram"
    (base / "home").mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    hook = flat[which]
    if hook is None:
        pytest.skip("base commit object not available for the differential")
    tripwires = [["tripwirepat", n, f"ls_ph{i}"] for i, n in enumerate(nudges or [])]
    scen = {"tripwires": tripwires, "chain": chain, "response": response, "terms": list(terms),
            "harness": harness, "ws4": ws4, "tool_use_id": tool_use_id, "raise_in": raise_in,
            "max_lines": max_lines, "session": SESSION, "command": command, "probe": True,
            "block_recall": block_recall,
            "recall_marker": str(base / "recall_reached")}
    sp, op = base / "scen.json", base / "out.json"
    sp.write_text(json.dumps(scen))
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(base / "home"), "ENGRAM_HOME": str(home)}
    argv = [sys.executable, str(flat["driver"]), str(hook), str(sp), str(op)]
    if kill_after is not None:
        proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(base))
        # Kill only once the stubbed daemon has been reached (deterministic on a
        # loaded box); kill_after is the hard upper bound, not the trigger.
        marker = base / "recall_reached"
        deadline = time.monotonic() + kill_after
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        reached = marker.exists()
        if proc.poll() is None:
            proc.kill()
        _, err = proc.communicate(timeout=10)
        assert reached, f"the hook never reached the recall daemon within {kill_after}s; stderr:\n{err[-1500:]}"
        return Run({"stdout": "", "rc": proc.returncode}, home, None, proc.returncode)
    cp = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10, cwd=str(base))
    assert op.exists(), f"driver crashed rc={cp.returncode}\n{cp.stderr[-1500:]}"
    res = json.loads(op.read_text())
    ctx = None
    if res["stdout"].strip():
        ctx = json.loads(res["stdout"])["hookSpecificOutput"]["additionalContext"]
    return Run(res, home, ctx, res["rc"])


def budget_of(harness):
    return 9500 if harness == "claude-code" else 4000


HARNESSES = ["claude-code", None]


def first_line(ctx):
    return ctx.split("\n", 1)[0]


# ============================================================ C1: total <= budget
@pytest.mark.parametrize("harness", HARNESSES)
def test_c1_block_near_budget_plus_chain_plus_recall(flat, tmp_path, harness):
    B = budget_of(harness)
    r = run(flat, tmp_path, harness=harness, nudges=[nudge_for_block(B - 400)],
            chain=chain_block(2, 90), response=response_for(_ids(1, 2, 3), 200))
    assert r.ctx is not None and len(r.ctx) <= B
    assert "NUDGE" in r.ctx


@pytest.mark.parametrize("harness", HARNESSES)
def test_c1_block_alone_at_budget(flat, tmp_path, harness):
    B = budget_of(harness)
    r = run(flat, tmp_path, harness=harness, nudges=[nudge_for_block(B)])
    assert len(r.ctx) == B and "tail cut" not in r.ctx


@pytest.mark.parametrize("harness", HARNESSES)
def test_c1_chain_alone_over(flat, tmp_path, harness):
    B = budget_of(harness)
    r = run(flat, tmp_path, harness=harness, nudges=[nudge_for_block(B - 300)],
            chain=chain_block(4, 150))
    assert len(r.ctx) <= B


@pytest.mark.parametrize("harness", HARNESSES)
def test_c1_recall_alone_over(flat, tmp_path, harness):
    B = budget_of(harness)
    n = B // 1000 + 2  # more lines than fit
    r = run(flat, tmp_path, harness=harness, response=response_for(_ids(*range(1, n + 1)), 1400),
            max_lines=n)
    assert r.ctx is not None and len(r.ctx) <= B


@pytest.mark.parametrize("harness", HARNESSES)
def test_c1_all_three_over(flat, tmp_path, harness):
    B = budget_of(harness)
    r = run(flat, tmp_path, harness=harness, nudges=[nudge_for_block(B - 700)],
            chain=chain_block(5, 200), response=response_for(_ids(1, 2, 3), 400))
    assert len(r.ctx) <= B


def _boundary(flat, tmp_path, extra, sub):
    """block + one recall line whose total is exactly budget + extra."""
    B = 9500
    nudge = nudge_for_block(B - 600)
    block_len = BLOCK_OVERHEAD + len(nudge)
    # total = block + "\n" + recall_text([line]) ; recall_text overhead fixed
    ref = recall_text([recall_line("ph_1", 100)])
    line_len = 100 + (B + extra - block_len - 1 - len(ref))
    resp = response_for(_ids(1), line_len)
    assert len(recall_text([recall_line("ph_1", line_len)])) + block_len + 1 == B + extra
    return run(flat, tmp_path, nudges=[nudge], response=resp, sub=sub), block_len, line_len


def test_c1_boundary_at_budget(flat, tmp_path):
    r, block_len, _ = _boundary(flat, tmp_path, 0, "b0")
    assert len(r.ctx) == 9500
    assert "tail cut" not in r.ctx and "ambient action-moment" in r.ctx


def test_c1_boundary_at_budget_plus_1(flat, tmp_path):
    r, block_len, _ = _boundary(flat, tmp_path, 1, "b1")
    assert len(r.ctx) <= 9500
    m = CLAUSE_RE.search(first_line(r.ctx))
    assert m and (m.group(1), m.group(2)) == ("1", "0")
    assert "ambient action-moment" not in r.ctx


# ============================================================ C2: differential
def _norm(text):
    return re.sub(r"[^\s\"(]*tripwire-overflow/\d+-\d+\.txt", "<overflow-path>", text)


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if k not in ("ts", "ts_iso", "nonce", "last_fire_ts")}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, str):
        return _norm(obj)
    return obj


def _snapshot(r):
    ws4 = []
    for row in r.ws4():
        row = dict(row)
        row.pop("chain_cut", None)  # the one additive field (C9), asserted separately
        ws4.append(row)
    return {"stdout": _norm(r.out["stdout"]), "rc": r.rc, "state": _scrub(r.state()),
            "shown": _scrub(r.shown()), "routes": _scrub(r.routes()),
            "fires": _scrub(r.fires()), "ws4": _scrub(ws4)}


def _fits_scenarios():
    out = []
    for harness in HARNESSES:
        B = budget_of(harness)
        out += [
            dict(harness=harness, nudges=["short nudge"]),
            dict(harness=harness, nudges=["short nudge"], chain=chain_block(2, 60)),
            dict(harness=harness, nudges=["short nudge"], response=response_for(_ids(1, 2), 120)),
            dict(harness=harness, nudges=["short nudge"], chain=chain_block(2, 60),
                 response=response_for(_ids(1, 2, 3), 150)),
            dict(harness=harness, response=response_for(_ids(1, 2, 3), 150)),
            dict(harness=harness, response=response_for(_ids(1), 100)),
            dict(harness=harness, nudges=[nudge_for_block(B - 1200)], chain=chain_block(1, 80),
                 response=response_for(_ids(1), 200)),
            dict(harness=harness, nudges=["a", "bb", "ccc"]),
            dict(harness=harness, nudges=["short nudge"], chain=chain_block(2, 60), ws4="seed-x",
                 tool_use_id="toolu_a"),
            dict(harness=harness, nudges=["short nudge"], chain=chain_block(2, 60), ws4="seed-x",
                 tool_use_id="toolu_b", response=response_for(_ids(1), 100)),
            dict(harness=harness, nudges=[nudge_for_block(B)]),  # block alone exactly at budget
            dict(harness=harness, nudges=[nudge_for_block(B - 600)],  # fits, within 64 of the budget
                 response=response_for(_ids(1), 600 - 1 - len(recall_text([])) - 20)),
            dict(harness=harness, nudges=["n" * 3000, "m" * 3000, "o" * 3000]),  # emit_context cuts
        ]
    return out


@pytest.mark.parametrize("i", range(26))
def test_c2_differential_byte_identical_when_everything_fits(flat, tmp_path, i):
    scens = _fits_scenarios()
    assert len(scens) == 26
    sc = scens[i]
    d = run(flat, tmp_path, which="dev", sub="dev", **sc)
    n = run(flat, tmp_path, which="new", sub="new", **sc)
    B = budget_of(sc["harness"])
    if d.ctx is not None:
        assert len(d.ctx) <= B, "scenario is not a fits-case"
    if "tripwire-overflow" in d.out["stdout"]:
        # only the run-dependent overflow path (dir + <ts>-<pid>) may differ
        assert _norm(n.out["stdout"]) == _norm(d.out["stdout"])
    else:
        assert n.out["stdout"] == d.out["stdout"], "stdout must be byte-identical"
    assert _snapshot(n) == _snapshot(d)
    for row in n.ws4():
        assert row.get("chain_cut") is False


# ============================================================ C3: order and units
def _cut_scenario(flat, tmp_path, *, chain=None, nlines=3, line_len=900, block_len=7600, sub="c3",
                  harness="claude-code", **kw):
    nudge = nudge_for_block(block_len)
    ids = _ids(*range(1, nlines + 1))
    resp = response_for(ids, line_len)
    lines = [recall_line(i, line_len) for i in ids]
    r = run(flat, tmp_path, harness=harness, nudges=[nudge], chain=chain, response=resp, sub=sub,
            max_lines=nlines, **kw)
    return r, nudge, lines, ids


def test_c3_order_recall_goes_before_chain(flat, tmp_path):
    chain = chain_block(2, 100)
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, chain=chain, block_len=7000, line_len=800)
    block_len = BLOCK_OVERHEAD + len(nudge)
    room = B - (block_len + 1 + len(chain)) - 1
    assert len(recall_text(lines)) > room  # the recall really has to be cut
    k = expected_keep(lines, room - 64)
    assert 0 < k < 3
    assert chain in r.ctx, "chain must survive while recall lines are being cut"
    for gone in ids[k:]:
        assert f"[{gone}]" not in r.ctx
    for kept in ids[:k]:
        assert f"[{kept}]" in r.ctx


def test_c3_order_chain_before_never_the_block_is_cut_whole(flat, tmp_path):
    chain = chain_block(3, 400)
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, chain=chain, block_len=B - 900, line_len=500)
    assert chain not in r.ctx and "[triggered principles]" not in r.ctx
    assert "  · c" not in r.ctx  # no partial chain line either
    assert f"  • {nudge}" in r.ctx and len(r.ctx) <= B


def test_c3_order_tripwire_block_text_untouched_when_a_cut_happens(flat, tmp_path):
    chain = chain_block(3, 400)
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, chain=chain, block_len=7000, line_len=900)
    block = f"{TRAILER0}\n{HEADER}\n  • {nudge}"
    first, rest = r.ctx.split("\n", 1)
    stripped = CLAUSE_RE.sub("", first)
    assert stripped == TRAILER0
    assert stripped + "\n" + rest.split("\n[triggered")[0].split("\n[ENGRAM")[0] == block


def test_c3_order_whole_line_cut_last_id_line_goes_first(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=7000, line_len=900)
    block_len = BLOCK_OVERHEAD + len(nudge)
    k = expected_keep(lines, B - 64 - block_len - 1)
    assert 0 < k < 3
    recall = r.ctx[r.ctx.index("[ENGRAM in-turn"):]
    assert recall == recall_text(lines[:k])
    for ln in r.ctx.split("\n"):
        if ln.startswith("  - ["):
            assert ln in lines, "a recall line was truncated mid-line"


def test_c3_order_all_id_lines_cut_leaves_no_header_or_footer(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=B - 150, line_len=400)
    assert "[ENGRAM in-turn recall" not in r.ctx
    assert "ambient action-moment" not in r.ctx
    assert "  - [" not in r.ctx
    assert CLAUSE_RE.search(first_line(r.ctx)).groups() == ("3", "0")


# ============================================================ C4: state is written for survivors
def test_c4_state_recent_ids_only_survivors_and_cut_id_shown_again(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=7000, line_len=900, sub="s")
    k = expected_keep(lines, B - 64 - (BLOCK_OVERHEAD + len(nudge)) - 1)
    assert 0 < k < 3
    assert r.state()["recent_ids"] == ids[:k]
    # next fire (same ENGRAM_HOME, new terms, a small block => room): the cut ids come back
    base = tmp_path / "s"
    sp = base / "scen2.json"
    scen = json.loads((base / "scen.json").read_text())
    scen["tripwires"] = []
    scen["terms"] = ["gammaterm"]
    scen["max_lines"] = 3
    sp.write_text(json.dumps(scen))
    op = base / "out2.json"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(base / "home"), "ENGRAM_HOME": str(base / "engram")}
    subprocess.run([sys.executable, str(flat["driver"]), str(flat["new"]), str(sp), str(op)],
                   env=env, capture_output=True, text=True, timeout=10, cwd=str(base), check=True)
    ctx2 = json.loads(json.loads(op.read_text())["stdout"])["hookSpecificOutput"]["additionalContext"]
    for cut_id in ids[k:]:
        assert f"[{cut_id}]" in ctx2, "a cut id must be shown again on a later fire"
    for kept in ids[:k]:
        assert f"[{kept}]" not in ctx2


def test_c4_route_ledger_and_shown_record_only_survivors_cut_named_tail_budget(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=7000, line_len=900, sub="rl")
    k = expected_keep(lines, B - 64 - (BLOCK_OVERHEAD + len(nudge)) - 1)
    assert 0 < k < 3
    route = r.routes()
    assert len(route) == 1
    assert route[0]["shown"] == ids[:k]
    for cut_id in ids[k:]:
        assert route[0]["cut"][cut_id] == "tail_budget"
    assert r.shown()[-1]["ids"] == ids[:k]


def test_c4_fire_ledger_lists_survivors_outcome_rendered(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=7000, line_len=900, sub="fl")
    k = expected_keep(lines, B - 64 - (BLOCK_OVERHEAD + len(nudge)) - 1)
    row = r.fires()[-1]
    assert row["rendered_ids"] == ids[:k] and row["outcome"] == "rendered"


def test_c4_all_cut_outcome_budget_cut_returns_empty_and_counts_the_fire(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=B - 150, line_len=400, sub="ac")
    row = r.fires()[-1]
    assert row["outcome"] == "budget_cut" and row["rendered_ids"] == []
    assert r.state()["last_fire_ts"] > 0 and r.state().get("recent_term_sets")
    assert r.state().get("recent_ids", []) == []
    route = r.routes()[0]
    assert route["shown"] == [] and all(v == "tail_budget" for v in route["cut"].values())
    assert r.shown()[-1]["ids"] == []


# ============================================================ C5: the marker
def test_c5_marker_in_trailer_exact_counts_and_path_stays_last(flat, tmp_path):
    # emit_context cuts hits (path in the trailer) AND the pass cuts the recall
    B = 9500
    nudges = ["n" * 800, "m" * 800, "o" * 800]
    r0 = run(flat, tmp_path, nudges=nudges, sub="m0")
    block_len = len(r0.ctx)
    line_len = 1500
    ids = _ids(1, 2, 3)
    lines = [recall_line(i, line_len) for i in ids]
    # shrink room: pad the chain so recall must be cut
    chain = chain_block(1, B - block_len - 64 - 1 - 1 - 2400)
    r = run(flat, tmp_path, nudges=nudges, chain=chain, response=response_for(ids, line_len),
            sub="m1", max_lines=3)
    fl = first_line(r.ctx)
    assert TRAILER_RE.match(fl)
    m = CLAUSE_RE.search(fl)
    assert m is not None
    k = expected_keep(lines, B - 64 - (block_len + 1 + len(chain)) - 1)
    assert m.groups() == (str(3 - k), "0")
    assert len(r.ctx) <= B
    # the clause sits after "cut" and before any head/see text; path capture stays last
    before = fl[: m.start()]
    assert re.fullmatch(r"▸ \d+ shown · \d+ cut", before)
    tail = fl[m.end():]
    if " — see " in tail:
        assert re.fullmatch(r" — see (\S+).*", tail)


def test_c5_marker_chain_cut_counts_chain_lines(flat, tmp_path):
    chain = chain_block(3, 400)  # 4 lines
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, chain=chain, block_len=9500 - 900, line_len=500,
                                         sub="mc")
    m = CLAUSE_RE.search(first_line(r.ctx))
    assert m and m.group(2) == "4"


def test_c5_marker_recall_only_shape_gets_its_own_first_line(flat, tmp_path):
    B = 4000
    n = 4
    ids = _ids(*range(1, n + 1))
    r = run(flat, tmp_path, harness=None, response=response_for(ids, 1300), max_lines=n, sub="ro")
    fl = first_line(r.ctx)
    m = OWN_LINE_RE.match(fl)
    assert m, fl
    lines = [recall_line(i, 1300) for i in ids]
    k = expected_keep(lines, B - 64)  # no other part: the whole budget less the reserve
    assert m.groups() == (str(n - k), "0")
    assert len(r.ctx) <= B


def test_c5_marker_worst_case_clause_fits_the_reserve():
    mod = _HOOK_CONSTS
    assert mod._TAIL_CUT_RESERVE == 64
    for big in (9, 99, 999, 99999, 999999):
        clause = mod._tail_cut_clause(big, big)
        own = mod._tail_cut_clause(big, big, own_line=True)
        assert len(clause) <= mod._TAIL_CUT_RESERVE
        assert len(own) + 1 <= mod._TAIL_CUT_RESERVE  # + the joining newline
    assert mod._tail_cut_clause(0, 0).startswith(" · tail cut: 0 recall lines, 0 chain lines")


@pytest.mark.parametrize("gap", [5, 30, 60])
def test_c5_marker_corner_block_within_64_of_budget_keeps_c1_and_c5(flat, tmp_path, gap):
    B = 9500
    r = run(flat, tmp_path, nudges=[nudge_for_block(B - gap)], response=response_for(_ids(1), 300),
            sub=f"k{gap}")
    assert len(r.ctx) <= B
    assert CLAUSE_RE.search(first_line(r.ctx)).groups() == ("1", "0")
    assert "NUDGE" in r.ctx and TRAILER_RE.match(first_line(r.ctx))


# ============================================================ C6: the arm draw
def _draw(seed, nonce):
    return "chain" if int(hashlib.sha256(f"{seed}:{nonce}".encode()).hexdigest(), 16) & 1 else "control"


def _nonce_for(seed, arm):
    for i in range(5000):
        n = f"toolu_c6_{i}"
        if _draw(seed, n) == arm:
            return n
    raise AssertionError


def test_c6_arm_draw_unchanged_by_a_cut_and_a_cut_chain_arm_row_says_chain(flat, tmp_path):
    seed = "seed-c6"
    nonce = _nonce_for(seed, "chain")
    chain = chain_block(3, 400)
    B = 9500
    nudge = nudge_for_block(B - 900)
    cut = run(flat, tmp_path, nudges=[nudge], chain=chain, ws4=seed, tool_use_id=nonce, sub="cut")
    fit = run(flat, tmp_path, nudges=["tiny"], chain=chain, ws4=seed, tool_use_id=nonce, sub="fit")
    assert cut.ws4()[0]["arm"] == fit.ws4()[0]["arm"] == "chain" == _draw(seed, nonce)
    assert cut.ws4()[0]["draw_basis"] == fit.ws4()[0]["draw_basis"] == "tool_use_id"
    nonce_c = _nonce_for(seed, "control")
    ctl = run(flat, tmp_path, nudges=[nudge], chain=chain, ws4=seed, tool_use_id=nonce_c, sub="ctl")
    assert ctl.ws4()[0]["arm"] == "control" and ctl.ws4()[0]["chain_cut"] is False


# ============================================================ C7: the #3674 guard stays
def test_c7_guard_stays():
    text = (HOOK_SRC_DIR / HOOK_NAME).read_text()
    assert "_fits = len(additionalContext) <= get_harness_budgets(" in text
    assert "_pending_shown[\"ids\"] if _fits else []" in text


def test_c7_shown_record_equals_survivors(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=7000, line_len=900, sub="c7")
    k = expected_keep(lines, B - 64 - (BLOCK_OVERHEAD + len(nudge)) - 1)
    assert r.shown()[-1]["ids"] == ids[:k] and 0 < k < 3


# ============================================================ C9: WS4 row
def test_c9_ws4_chain_cut_true_only_on_a_cut(flat, tmp_path):
    seed = "seed-c9"
    nonce = _nonce_for(seed, "chain")
    chain = chain_block(3, 400)
    B = 9500
    cut = run(flat, tmp_path, nudges=[nudge_for_block(B - 900)], chain=chain, ws4=seed, tool_use_id=nonce,
              sub="cut")
    row = cut.ws4()[0]
    assert row["chain_cut"] is True
    assert row["chain_chars"] == len(chain), "chain_chars stays the WOULD-RENDER length"
    assert row["chain_rendered"] is False
    fit = run(flat, tmp_path, nudges=["tiny"], chain=chain, ws4=seed, tool_use_id=nonce, sub="fit")
    frow = fit.ws4()[0]
    assert frow["chain_cut"] is False and frow["chain_rendered"] is True
    assert frow["chain_chars"] == len(chain)
    nonce_c = _nonce_for(seed, "control")
    ctl = run(flat, tmp_path, nudges=["tiny"], chain=chain, ws4=seed, tool_use_id=nonce_c, sub="ctl")
    crow = ctl.ws4()[0]
    assert crow["chain_cut"] is False and crow["chain_rendered"] is False


# ============================================================ C10: fail-open
def test_c10_failopen_a_raising_pass_still_prints_dev_shaped_output(flat, tmp_path):
    B = 9500
    r, nudge, lines, ids = _cut_scenario(flat, tmp_path, block_len=7000, line_len=900, sub="fo",
                                         raise_in="_apply_tail_cut")
    assert r.rc == 0
    assert r.ctx is not None and f"  • {nudge}" in r.ctx
    assert "tail cut" not in r.ctx


# ============================================================ corner with several hits
@pytest.mark.parametrize("n_hits", [2, 3])
@pytest.mark.parametrize("gap", [5, 60])
def test_c5_corner_several_hits_chain_never_cites_a_hidden_lesson(flat, tmp_path, n_hits, gap):
    """The corner re-render (budget - reserve) leaves the cut set of the first render
    unchanged, and no chain can ride along that close to the budget: the chain is
    dropped whole and the WS4 row still names exactly the one visible hit as shown."""
    seed = "seed-corner"
    nonce = _nonce_for(seed, "chain")
    nudges = [nudge_for_block(9500 - gap)] + [f"small hit {i} " + "z" * 20 for i in range(1, n_hits)]
    chain = "[triggered principles]\n  ↑ ls_ph0 cite\n  ↑ ls_ph1 cite"
    r = run(flat, tmp_path, nudges=nudges, chain=chain, response=response_for(_ids(1), 300),
            ws4=seed, tool_use_id=nonce, sub=f"cs{n_hits}{gap}")
    assert len(r.ctx) <= 9500
    assert "[triggered principles]" not in r.ctx and "cite" not in r.ctx
    assert "NUDGE" in r.ctx and "small hit" not in r.ctx  # one hit shown, the rest cut as before
    row = r.ws4()[0]
    assert row["arm"] == "chain" and row["shown_lesson_ids"] == ["ls_ph0"]
    assert row["chain_cut"] is True and row["chain_rendered"] is False
    assert CLAUSE_RE.search(first_line(r.ctx)).groups() == ("1", str(chain.count("\n") + 1))


# ============================================================ WS4 row kill-safety (round 1 review)
def _chain_arm_scenario(B=9500):
    seed = "seed-ks"
    return dict(nudges=["short nudge"], chain=chain_block(2, 60), ws4=seed,
                tool_use_id=_nonce_for(seed, "chain"), response=response_for(_ids(1), 200))


def test_ks_kill_during_recall_leaves_the_same_ws4_rows_as_dev(flat, tmp_path):
    """A hook killed while the recall daemon blocks has already decided the chain:
    its WS4 row must exist, exactly as on the base commit."""
    d = run(flat, tmp_path, which="dev", sub="kd", block_recall=True, kill_after=30, **_chain_arm_scenario())
    n = run(flat, tmp_path, which="new", sub="kn", block_recall=True, kill_after=30, **_chain_arm_scenario())
    assert len(d.ws4()) == 1
    assert len(n.ws4()) == len(d.ws4())
    assert n.ws4()[0]["arm"] == "chain" and n.ws4()[0]["chain_cut"] is False


def test_ks_non_kill_run_has_the_same_row_count_as_dev(flat, tmp_path):
    for i, extra in enumerate([{}, {"response": None}]):
        sc = {**_chain_arm_scenario(), **extra}
        d = run(flat, tmp_path, which="dev", sub=f"nd{i}", **sc)
        n = run(flat, tmp_path, which="new", sub=f"nn{i}", **sc)
        assert len(n.ws4()) == len(d.ws4()) == 1


def _sliver(flat, tmp_path, *, response, sub, **kw):
    """block 8,700 + chain: block + chain = 9,490 (within the reserve of 9,500)."""
    seed = "seed-sl"
    block = nudge_for_block(8700)
    chain = chain_block(1, 762)
    assert 8700 + 1 + len(chain) == 9490
    return run(flat, tmp_path, nudges=[block], chain=chain, ws4=seed,
               tool_use_id=_nonce_for(seed, "chain"), response=response, sub=sub, **kw), chain


def test_ks_sliver_late_chain_drop_ends_with_exactly_one_patched_row(flat, tmp_path):
    r, chain = _sliver(flat, tmp_path, response=response_for(_ids(1), 300), sub="sl1")
    assert len(r.ws4()) == 1
    row = r.ws4()[0]
    assert row["chain_cut"] is True and row["chain_rendered"] is False
    assert row["chain_chars"] == len(chain)


def test_ks_sliver_without_a_late_drop_keeps_one_unpatched_row(flat, tmp_path):
    r, chain = _sliver(flat, tmp_path, response=None, sub="sl2")
    assert len(r.ws4()) == 1
    assert r.ws4()[0]["chain_cut"] is False and r.ws4()[0]["chain_rendered"] is True
    assert chain in r.ctx


def test_chain_goes_when_the_marker_would_not_fit_after_the_recall_is_gone(flat, tmp_path):
    """Priority is block, then chain, then recall; the chain also goes when the
    marker would not fit once the recall is gone (never truncate a lesson hit)."""
    r, chain = _sliver(flat, tmp_path, response=response_for(_ids(1), 300), sub="pri")
    assert len(r.ctx) <= 9500
    assert chain not in r.ctx and "[triggered principles]" not in r.ctx
    assert CLAUSE_RE.search(first_line(r.ctx)).groups() == ("1", "2")
    assert "…[truncated" not in r.ctx, "the lesson hit must not be truncated to keep the chain"
    row = r.ws4()[0]
    assert row["chain_cut"] is True and row["chain_rendered"] is False
