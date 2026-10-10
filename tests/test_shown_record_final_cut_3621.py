"""Contract-first tests: the shown record is ALL-OR-NOTHING on emit_context's final cut (#3621).

The surface hook renders ONE recall block among several body blocks, and
`_hooklib.emit_context()` then drops whole tail blocks that do not fit the
harness budget. The shown record (`sessions/<sid>.shown.jsonl`) must therefore
list the fire's ids only when the recall block survived IN FULL; otherwise the
fire still lands a line, with an EMPTY ids list (the fail direction is
under-recording: a repeated display is cheap, a recorded-but-unseen id is a
recall loss once exclusion is on).

S1  emit_context(..., kept_out=[]) extends the list with the 0-based indices (into
    the body as passed) of items rendered IN FULL. Cut / truncated items are not
    in it. The returned string is byte-identical with and without kept_out.
    A non-list kept_out is ignored; emit_context never raises because of it.
S2  _assemble_response(head, body, kept_out=None) threads kept_out through.
S3  main(): recall block index in kept_out -> full shown ids; else an EMPTY-ids
    line (so prompt_index stays continuous).
S4  the record is appended only AFTER the response was printed; a print that
    raises records nothing.
S5  the route ledger still gets the route's own `shown` (a different question).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))
HOOK_DIR = SRC_ENGRAM / "hooks" / "claude"
SURFACE_PATH = HOOK_DIR / "engram-surface-hook.py"
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))
import _hooklib  # noqa: E402,F401
from _hooklib import emit_context  # noqa: E402

SESSION = "sess-finalcut-1"
STARTED = "2026-10-09T00:00:00+00:00"
OLD = "2026-10-01T00:00:00+00:00"
BUDGET = 9500  # claude-code's hard budget (_harness.HARNESS_BUDGETS)


def _ids(*nums, prefix="ob"):
    # built, never spelled: the repo's no-new-shipped-node-ids gate scans tests too
    return [f"{prefix}_{n}" for n in nums]


# ======================================================================
# S1 -- emit_context kept_out
# ======================================================================

_TRAILER_RE = re.compile(r"^▸ (\d+) shown · (\d+) cut")


def _items(n, size):
    return [("item%d-" % i) + "x" * (size - 6) for i in range(n)]


def _oracle(items, out):
    """Independent oracle: an item was rendered in full iff it is a whole line."""
    lines = out.split("\n")
    return [i for i, it in enumerate(items) if it in lines]


# (label, head, items, budget, target, allow_single_overflow)
_GRID = [
    ("nothing_cut", "H", _items(3, 20), 400, 400, False),
    ("tail_cut", "H", _items(6, 100), 330, 330, False),
    ("tail_cut_exact_boundary", "", _items(4, 100), 4 * 100 + 3 + 40, 4 * 100 + 3 + 40, False),
    ("single_truncated", "H", _items(1, 1000), 300, 300, False),
    ("first_truncated_rest_cut", "H", _items(3, 1000), 300, 300, False),
    ("head_trimmed", "\n".join("headline %d " % i + "h" * 60 for i in range(8)),
     _items(2, 40), 300, 300, False),
    ("single_overflow_allowed", "H", _items(1, 400), 600, 300, True),
    ("single_overflow_refused", "H", _items(1, 400), 600, 300, False),
    ("single_overflow_over_budget", "H", _items(1, 900), 600, 300, True),
    ("overflow_allowed_tail_cut", "H", _items(3, 400), 600, 300, True),
    ("empty_body", "just a head", [], 300, 300, False),
]


def _both(head, items, budget, target, aso, **kw):
    plain = emit_context(head, list(items), budget=budget, target=target,
                         allow_single_overflow=aso, **kw)
    ko: list = []
    with_ko = emit_context(head, list(items), budget=budget, target=target,
                           allow_single_overflow=aso, kept_out=ko, **kw)
    return plain, with_ko, ko


@pytest.mark.parametrize("label,head,items,budget,target,aso", _GRID, ids=[g[0] for g in _GRID])
def test_s1_kept_out_matches_independent_oracle_and_output_is_byte_identical(
        label, head, items, budget, target, aso):
    """Mutations: counting a cut / truncated item as kept; off-by-one at the
    boundary; kept_out from the pre-cut item count. (On base the kept_out call
    raises TypeError, so the whole cell is red there.)"""
    plain, with_ko, ko = _both(head, items, budget, target, aso)
    assert with_ko == plain
    assert ko == _oracle(items, with_ko)
    assert len(with_ko) <= budget


def test_s1_explicit_expectations_pin_the_shapes():
    # nothing cut: every index
    _, _, ko = _both("H", _items(3, 20), 400, 400, False)
    assert ko == [0, 1, 2]
    # tail cut: kept is exactly the trailer's "N shown" prefix, and N < total
    plain, _, ko = _both("H", _items(6, 100), 330, 330, False)
    n_shown = int(_TRAILER_RE.match(plain).group(1))
    assert 0 < n_shown < 6 and ko == list(range(n_shown))
    # a truncated item (the truncated_first shape) is NOT kept, though "1 shown"
    plain, _, ko = _both("H", _items(1, 1000), 300, 300, False)
    assert _TRAILER_RE.match(plain).group(1) == "1" and "…[truncated" in plain
    assert ko == []
    plain, _, ko = _both("H", _items(3, 1000), 300, 300, False)
    assert "…[truncated" in plain and ko == []
    # allow_single_overflow True keeps the item whole; False truncates it
    assert _both("H", _items(1, 400), 600, 300, True)[2] == [0]
    assert _both("H", _items(1, 400), 600, 300, False)[2] == []
    # empty body
    assert _both("just a head", [], 300, 300, False)[2] == []


def test_s1_exact_boundary_item_counts_on_the_right_side():
    """Sweep the budget one char at a time across every cut boundary: kept always
    equals the oracle (off-by-one at the cut boundary)."""
    items = _items(4, 100)
    seen = set()
    for b in range(60, 520):
        plain, with_ko, ko = _both("", items, b, b, False)
        assert with_ko == plain and ko == _oracle(items, with_ko), b
        seen.add(len(ko))
    assert {0, 1, 2, 3, 4} <= seen  # the sweep really crossed every boundary


def test_s1_works_with_overflow_writer_paths():
    calls = []

    def writer(cut):
        calls.append(list(cut))
        return "/tmp/overflow-3621.txt"

    items = _items(6, 100)
    plain, with_ko, ko = _both("H", items, 330, 330, False, overflow_writer=writer)
    assert with_ko == plain and ko == _oracle(items, with_ko) and calls


def test_s1_kept_out_none_default_changes_nothing():
    """(guard; green on base only if kept_out were accepted -- it is not, so red there)"""
    items = _items(6, 100)
    assert emit_context("H", items, budget=330, target=330) == \
        emit_context("H", items, budget=330, target=330, kept_out=None)


@pytest.mark.parametrize("bad", [(), "x", 7, {}, object(), (1, 2)])
def test_s1_non_list_kept_out_is_ignored_not_an_error(bad):
    items = _items(6, 100)
    expect = emit_context("H", items, budget=330, target=330)
    assert emit_context("H", items, budget=330, target=330, kept_out=bad) == expect


def test_s1_kept_out_is_extended_not_replaced():
    ko = ["pre"]
    emit_context("H", _items(2, 20), budget=400, target=400, kept_out=ko)
    assert ko == ["pre", 0, 1]


def test_s1_a_generator_body_is_indexed_as_passed():
    items = _items(3, 20)
    ko: list = []
    emit_context("H", (x for x in items), budget=400, target=400, kept_out=ko)
    assert ko == [0, 1, 2]


# ======================================================================
# S2 -- _assemble_response threads kept_out
# ======================================================================

@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    eh = tmp_path / "engram"
    (eh / "sessions").mkdir(parents=True)
    monkeypatch.setenv("ENGRAM_HOME", str(eh))
    yield
    mod = sys.modules.get("engram_idf")
    if mod is not None and getattr(mod, "__file__", None) is None:
        sys.modules.pop("engram_idf", None)


def _eh() -> Path:
    return Path(os.environ["ENGRAM_HOME"])


def _load_surface(tag):
    spec = importlib.util.spec_from_file_location(f"surface_finalcut_{tag}", SURFACE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_s2_assemble_response_threads_kept_out_and_return_is_unchanged(monkeypatch):
    """The plain-call half is a pass-through guard (green on base); the kept_out
    arm is red on base (no such parameter)."""
    mod = _load_surface("s2")
    monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")
    mid, tail = "M" * (BUDGET - 400), "T" * 500
    heads, body = ["HEAD"], ["small block", mid, tail]
    plain = mod._assemble_response(heads, body)
    ctx = plain["hookSpecificOutput"]["additionalContext"]
    assert "small block" in ctx and mid in ctx.split("\n") and tail not in ctx  # tail cut
    ko: list = []
    assert mod._assemble_response(heads, body, ko) == plain
    assert ko == [0, 1]
    ko2: list = []
    assert mod._assemble_response([], [], ko2) == {} and ko2 == []
    assert mod._assemble_response(heads, body, kept_out=None) == plain


# ======================================================================
# S3 / S4 / S5 -- main()
# ======================================================================

def _marker(session=SESSION):
    (_eh() / "sessions" / f"{session}.json").write_text(json.dumps(
        {"session_id": session, "started_at": STARTED, "transcript_path": ""}))


def _record(session=SESSION):
    p = _eh() / "sessions" / f"{session}.shown.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _entry(nid):
    return {"id": nid, "type": "observation", "claim": f"claim {nid}", "confidence": 0.85,
            "created_at": OLD, "created_ago": "5d", "created_minutes_ago": 7200,
            "recall_summary": f"summary {nid}", "recall_keywords": ["kw"]}


def _response(tops):
    return {"match_count": len(tops), "matched_ids": list(tops),
            "matched_meta": [_entry(i) for i in tops], "special_nodes": [],
            "top_claims": [_entry(i) for i in tops], "age": {}, "issues": {}}


class _RaisingOut(io.StringIO):
    def write(self, s):
        raise OSError("stdout closed")


def _run(mod, monkeypatch, response, *, principle="", warm="", stdout=None,
         prompt="how do we rank recall?", session=SESSION, ledger=None):
    monkeypatch.setattr(mod, "query_daemon", lambda *a, **kw: response)
    monkeypatch.setattr(mod, "query_fts_fallback", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_get_auto_surface_config", lambda: {
        "idf_gate_enabled": False, "short_prompt_threshold_chars": 0,
        "prev_response_tail_chars": 0})
    monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: (principle, []))
    monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
    monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
    monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
    monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
    monkeypatch.setattr(mod, "check_warm_briefing", lambda count: warm)
    monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
    monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))
    monkeypatch.setattr(mod, "_check_db_liveness", lambda *a: (True, "ok"))
    monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")
    if ledger is not None:
        monkeypatch.setattr(mod, "write_route_ledger", lambda *a, **kw: ledger.append(a))
    monkeypatch.setitem(sys.modules, "context_tracker", types.SimpleNamespace(
        estimate_usage=lambda *a, **kw: None, format_drowsiness=lambda *a, **kw: ""))
    stdin = json.dumps({"prompt": prompt, "session_id": session, "transcript_path": ""})
    out, err = (stdout if stdout is not None else io.StringIO()), io.StringIO()
    raised = None
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()))), \
            patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            mod.main()
        except SystemExit:
            pass
        except Exception as e:  # noqa: BLE001
            raised = e
    return (out.getvalue() if not isinstance(out, _RaisingOut) else ""), raised


def _shown_in(stdout):
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    if "[ENGRAM Recall" not in ctx:
        return []
    return re.findall(r"\[([a-z]{2}_\d+)\]", ctx[ctx.index("[ENGRAM Recall"):])


def test_s3_recall_block_fits_records_full_ids(monkeypatch):
    """(green on base; guards the fits case against an over-eager empty record)"""
    _marker()
    mod = _load_surface("s3a")
    out, _ = _run(mod, monkeypatch, _response(_ids(1, 2, 3)), principle="small principle block")
    rows = _record()
    assert len(rows) == 1 and rows[0]["ids"] == _shown_in(out) == _ids(1, 2, 3)


def test_s3_recall_block_cut_records_empty_ids_but_the_line_lands(monkeypatch):
    """Mutation: record full ids regardless of kept_out (red here)."""
    _marker()
    mod = _load_surface("s3b")
    out, _ = _run(mod, monkeypatch, _response(_ids(1, 2, 3)),
                  principle="P" * (BUDGET - 100))
    assert "[ENGRAM Recall" not in out  # the recall block really was cut
    rows = _record()
    assert len(rows) == 1 and rows[0]["ids"] == [] and rows[0]["route"] == "prompt_surface"


def test_s3_prompt_index_stays_continuous_across_a_cut_fire(monkeypatch):
    """Mutation: skip the append when cut (the cut fire leaves a hole: red here)."""
    _marker()
    mod = _load_surface("s3c")
    _run(mod, monkeypatch, _response(_ids(1)), prompt="first prompt here")
    _run(mod, monkeypatch, _response(_ids(2)), principle="P" * (BUDGET - 100),
         prompt="second prompt here")
    _run(mod, monkeypatch, _response(_ids(3)), prompt="third prompt here")
    rows = _record()
    assert [r["prompt_index"] for r in rows] == [0, 1, 2]
    assert [r["ids"] for r in rows] == [_ids(1), [], _ids(3)]


def test_s3_recall_kept_while_a_later_block_is_cut_still_records_full_ids(monkeypatch):
    """The recall block is not last: a huge warm-briefing AFTER it is the cut one.
    Mutation: use the last block's index instead of the recall block's (red here)."""
    _marker()
    mod = _load_surface("s3d")
    out, _ = _run(mod, monkeypatch, _response(_ids(1, 2)), warm="W" * (BUDGET - 100))
    assert "[ENGRAM Recall" in out and "W" * 100 not in out
    assert _record()[0]["ids"] == _ids(1, 2)


def test_s3_no_recall_block_records_an_empty_line(monkeypatch):
    """A fire where recall rendered nothing: the per-fire line still exists, empty."""
    _marker()
    mod = _load_surface("s3e")
    _run(mod, monkeypatch, None, principle="some principle block")
    rows = _record()
    assert len(rows) == 1 and rows[0]["ids"] == []


def test_s3_the_nudge_index_follows_the_blocks_before_it(monkeypatch):
    """The recall block sits at index 1 behind the principle block: kept with a small
    principle block, cut with a huge one. (A fixed index would flip a fire.)"""
    _marker()
    mod = _load_surface("s3f")
    _run(mod, monkeypatch, _response(_ids(1)), principle="small", prompt="one prompt here")
    _run(mod, monkeypatch, _response(_ids(2)), principle="P" * (BUDGET - 100),
         prompt="two prompt here")
    assert [r["ids"] for r in _record()] == [_ids(1), []]


def test_s5_route_ledger_keeps_the_routes_own_shown_even_when_cut(monkeypatch):
    """(ledger half green on base) the ledger records the ROUTE's render, unchanged."""
    _marker()
    mod = _load_surface("s5")
    ledger: list = []
    _run(mod, monkeypatch, _response(_ids(1, 2)), principle="P" * (BUDGET - 100),
         ledger=ledger)
    assert len(ledger) == 1 and ledger[0][5] == _ids(1, 2)
    assert _record()[0]["ids"] == []


def test_s4_a_print_that_raises_records_nothing(monkeypatch):
    """Mutation: append before the print (a line lands although nothing was written)."""
    _marker()
    mod = _load_surface("s4a")
    _, raised = _run(mod, monkeypatch, _response(_ids(1, 2)), stdout=_RaisingOut())
    assert isinstance(raised, OSError)
    assert _record() == []


def test_s4_the_record_is_written_after_the_print(monkeypatch):
    mod = _load_surface("s4b")
    _marker()
    order = []
    real_append = mod.append_shown_record
    monkeypatch.setattr(mod, "append_shown_record",
                        lambda *a, **kw: (order.append("append"), real_append(*a, **kw)))

    class _Out(io.StringIO):
        def write(self, s):
            if s.strip():
                order.append("print")
            return super().write(s)

    _run(mod, monkeypatch, _response(_ids(1)), stdout=_Out())
    assert order == ["print", "append"]


# ======================================================================
# Round 2 -- cells that pin the real-path shrink loop, the floor degrade and
# the lone truncated recall block (the three plausible-wrong mutants round 1
# did not catch).
# ======================================================================

_LONG_PATH = "/p/" + "d" * 152  # 155 chars: far longer than the sizing placeholder


def _distinct(n, size):
    """n items of exactly `size` chars with DISTINCT, non-overlapping texts. Each
    ends in a unique `<i>` tag, so a truncated copy can never contain the whole
    item (whole-item substring <=> item rendered in full)."""
    return [chr(65 + i) * (size - 3) + "<%d>" % i for i in range(n)]


@pytest.fixture
def _fixed_placeholder(monkeypatch):
    """The sizing placeholder embeds ENGRAM_HOME (read at import) and the pid, so its
    length varies by machine; pin it to 56 chars so the shrink cells are deterministic."""
    monkeypatch.setattr(_hooklib, "_placeholder_overflow_path",
                        lambda: "/h/hook-overflow/" + "9" * 39)


def _long_writer(cut):
    return _LONG_PATH


def _stage2_shown(head, items, budget, target):
    """The shown count Stage 2 picks BEFORE the shrink loop: a writer returning a
    path exactly as long as the sizing placeholder leaves nothing to shrink."""
    ph = _hooklib._placeholder_overflow_path()
    out = emit_context(head, list(items), budget=budget, target=target,
                       overflow_writer=lambda cut: ph)
    return int(_TRAILER_RE.match(out).group(1))


def _shrunk_case(head, items, budget, target):
    plain = emit_context(head, list(items), budget=budget, target=target,
                         overflow_writer=_long_writer)
    ko: list = []
    with_ko = emit_context(head, list(items), budget=budget, target=target,
                           overflow_writer=_long_writer, kept_out=ko)
    return plain, with_ko, ko


def _assert_whole_item_contract(items, plain, with_ko, ko):
    assert with_ko == plain  # byte-identical with and without kept_out
    in_out = [i for i, it in enumerate(items) if it in with_ko]
    assert ko == in_out
    for i, it in enumerate(items):
        if i not in ko:
            assert it not in with_ko, i


def test_t1_shrink_loop_two_of_four_items_kept_out_follows_the_post_shrink_count(_fixed_placeholder):
    """The real (155-char) path pushes the render over target; the loop shrinks
    3 -> 2 (stops short of the floor). The fire is PROVEN to shrink by comparing
    against Stage 2's own pre-shrink count (same call with a placeholder-length
    path). MUST REDDEN: kept_out sized from the Stage-2 `shown_n` snapshotted
    BEFORE the shrink loop (it would report [0, 1, 2], over-reporting item 2)."""
    items = _distinct(4, 100)
    plain, with_ko, ko = _shrunk_case("H", items, 400, 400)
    pre = _stage2_shown("H", items, 400, 400)
    post = int(_TRAILER_RE.match(with_ko).group(1))
    assert pre == 3 and post == 2  # the loop fired and stopped at 2 (>= 2, < 3)
    _assert_whole_item_contract(items, plain, with_ko, ko)
    assert ko == [0, 1] and items[2] not in with_ko and len(with_ko) <= 400


def test_t1_shrink_loop_down_to_one_item_that_is_rendered_in_full(_fixed_placeholder):
    """3 items, budget == target == 300: Stage 2 keeps 2, the real path shrinks the
    loop to 1, and that one item still fits whole (so it is kept). PROVEN to fire
    via Stage 2's pre-shrink count. MUST REDDEN: kept_out from the pre-shrink
    `shown_n` ([0, 1] -- item 1 is NOT in the output)."""
    items = _distinct(3, 100)
    plain, with_ko, ko = _shrunk_case("H", items, 300, 300)
    pre = _stage2_shown("H", items, 300, 300)
    post = int(_TRAILER_RE.match(with_ko).group(1))
    assert pre == 2 and post == 1
    assert "…[truncated" not in with_ko  # rendered whole, not the truncated_first shape
    _assert_whole_item_contract(items, plain, with_ko, ko)
    assert ko == [0] and items[0] in with_ko and len(with_ko) <= 300


def test_t2_floor_degrade_to_truncated_first_reports_nothing_kept(_fixed_placeholder):
    """3 items of 300 chars, target 450: Stage 2 keeps 1 (placeholder path fits), and
    against the real 155-char path even that one item does not fit, so Stage 3 takes
    its FLOOR-DEGRADE branch (truncated_first = True, a second writer call). The
    marker is in-band (the render stays under budget, so the final backstop did NOT
    fire). kept_out must be empty and the first item's full text must not be in the
    output. MUST REDDEN: leaving `truncated_first` False in the floor-degrade branch
    (the end check would count item 0 as kept)."""
    items = _distinct(3, 300)
    writes = []

    def writer(cut):
        writes.append(list(cut))
        return _LONG_PATH

    ko: list = []
    out = emit_context("H", list(items), budget=450, target=450,
                       overflow_writer=writer, kept_out=ko)
    assert _stage2_shown("H", items, 450, 450) == 1  # Stage 2 did NOT degrade by itself
    assert len(writes) == 2 and writes[1][0] == items[0]  # the floor-degrade rewrite ran
    assert "…[truncated, full text in " + _LONG_PATH + "]" in out  # in-band, not backstopped
    assert len(out) <= 450
    assert out == emit_context("H", list(items), budget=450, target=450,
                               overflow_writer=writer)
    assert ko == [] and items[0] not in out


def test_t2_loop_reaches_one_item_then_degrades_and_nothing_is_kept(_fixed_placeholder):
    """3 items of 80 chars, target 250: Stage 2 keeps 2, the shrink LOOP drops to 1, and
    that item still does not fit, so it degrades. (The marker then no longer fits the
    budget and the final backstop trims the render, so this cell is a guard on the
    loop-to-floor path; it does NOT redden the `truncated_first`-left-False mutant,
    because the backstop independently reports nothing kept. The cell above is the one
    that does. With `target == budget`, the only shape production passes, a
    loop-iterating degrade with an in-band marker is not reachable: the marker repeats
    the long path, costing more than the shrink saves. With `target < budget` it IS
    reachable (reviewer-fairy round 2 found 79 such cases in a synthetic sweep, e.g.
    budget 260, target 140, five 25-char items, a 100-char writer path), and that
    shape is not covered here.)"""
    items = _distinct(3, 80)
    plain, with_ko, ko = _shrunk_case("H", items, 250, 250)
    assert _stage2_shown("H", items, 250, 250) == 2
    assert with_ko == plain and ko == [] and items[0] not in with_ko


def test_t3_lone_truncated_recall_block_still_lands_an_empty_ids_line(monkeypatch):
    """Through main(): the recall block is the ONLY body block and is longer than
    the whole budget, so emit_context truncates it in-band (`_kept_blocks` is empty
    although a body exists). The fire's line must STILL be appended, with EMPTY ids,
    and the recall's shown ids must not be recorded; prompt_index stays continuous.
    MUST REDDEN: skipping the append when `_kept_blocks` is empty."""
    _marker()
    mod = _load_surface("t3")
    real_format_nudge = mod.format_nudge

    def _huge_nudge(*a, **kw):
        real_format_nudge(*a, **kw)  # fills route_out the real way
        kw["route_out"]["shown"] = _ids(1, 2, 3)
        return "[ENGRAM Recall]\n" + "R" * (BUDGET + 500)

    monkeypatch.setattr(mod, "format_nudge", _huge_nudge)
    out, raised = _run(mod, monkeypatch, _response(_ids(1, 2, 3)), prompt="first prompt here")
    assert raised is None
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "…[truncated" in ctx and "R" * (BUDGET + 500) not in ctx  # truncated in-band
    rows = _record()
    assert len(rows) == 1 and rows[0]["ids"] == [] and rows[0]["prompt_index"] == 0
    # a following fire continues the index (no hole where the lone-truncated fire was)
    monkeypatch.setattr(mod, "format_nudge", real_format_nudge)
    _run(mod, monkeypatch, _response(_ids(4)), prompt="second prompt here")
    assert [r["prompt_index"] for r in _record()] == [0, 1]
    assert [r["ids"] for r in _record()] == [[], _ids(4)]
