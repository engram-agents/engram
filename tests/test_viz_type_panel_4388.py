"""Contract tests for the viz "Node types" panel (engram-alpha#4388).

Contract (design-update comment on #4388, relaying Lei 2026-11-05):

C1. Type chips are PLAIN TOGGLES (no solo-on-first-click, from any state).
C2. "Hide all": ``activeTypes`` becomes empty, every chip dimmed, one render().
C3. "Show all": ``activeTypes`` becomes every ``NODE_STYLE`` key, no chip
    dimmed, one render().
C4. ``filteredData()`` has NO backbone bypass: a node survives iff (legible
    mode: its id is in ``legibleNodeIds()``) AND its type is active AND
    (``showSuperseded`` or ``is_current``).  Hidden types are hidden in BOTH
    modes, backbone types included.
C5. ``legibleNodeIds()`` is unchanged (the backbone stays in the default
    subset).
C6. The two buttons are static markup in graph.html inside the "Node types"
    section, outside ``#type-filters``; ``buildSidebar()`` (called more than
    once) wires them with ``.onclick =`` assignment, never creating them.
C7 (empty canvas, no console error) is NOT asserted here: these cells cannot
    run d3.  The live Playwright run covers it.

Harness: like ``tests/test_health_workbench_draft_persist.py``, the real
function source text is brace-matched out of the served ``graph.js`` and run
under ``node -e`` against stub globals; ``graph.js`` itself is not
``require()``-able whole (top-level DOM and d3).

Held-constant grid: MODE (legible/full) x ACTIVE SET (all / only a backbone
type / only a non-backbone type / backbone + non-backbone / empty) x GRAPH
SIZE (<= LEGIBLE_SMALL_GRAPH vs more) x SUPERSEDED FLAG x CALL COUNT of
buildSidebar (1 vs 2).  TRAP: at or below LEGIBLE_SMALL_GRAPH the legible set
is EVERYTHING, so legible and full look identical and a bypass is invisible;
every legible-mode cell therefore uses the LARGE graph, which has more
non-backbone nodes than LEGIBLE_RECENT_LIMIT (asserted below).

Held constant (named): ORDER OF EFFECTS: state is assigned before render()
reads it.  The ``render`` stub records, at every call, the sorted
``activeTypes`` and the sorted dimmed chip keys it SEES (``renderSeen``), so
every cell that claims a render happened also claims WHAT it saw; a handler
that calls render() before changing state (the canvas would draw the previous
filter) is red.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS_PATH = _ROOT / "src" / "engram" / "viz" / "static" / "graph.js"
_HTML_PATH = _ROOT / "src" / "engram" / "viz" / "templates" / "graph.html"

_NODE_AVAILABLE = shutil.which("node") is not None

JS_TEXT = _JS_PATH.read_text(encoding="utf-8")
HTML_TEXT = _HTML_PATH.read_text(encoding="utf-8")


def _extract_function_source(js_text: str, func_name: str) -> str:
    """Brace-matched extraction of one top-level function's full source."""
    start = js_text.index("function " + func_name + "(")
    i = js_text.index("{", start)
    depth = 0
    while i < len(js_text):
        c = js_text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return js_text[start : i + 1]
        i += 1
    raise AssertionError("unbalanced braces while extracting " + func_name)


def _extract_const_line(js_text: str, name: str) -> str:
    m = re.search(r"^const " + re.escape(name) + r" = [^\n]*;$", js_text, re.M)
    assert m, "const " + name + " not found as a one-line declaration"
    return m.group(0)


RECENT = int(re.search(r"const LEGIBLE_RECENT_LIMIT = (\d+);", JS_TEXT).group(1))
SMALL = int(re.search(r"const LEGIBLE_SMALL_GRAPH = (\d+);", JS_TEXT).group(1))
BACKBONE = tuple(
    re.findall(
        r"'(\w+)'",
        re.search(r"const BACKBONE_TYPES = new Set\(\[([^\]]*)\]\)", JS_TEXT).group(1),
    )
)
OTHER_TYPES = ("observation", "derivation", "lesson", "conjecture", "question")
ALL_TYPES = BACKBONE + OTHER_TYPES
NODE_STYLE = {t: {"color": "#123456", "label": t.title()} for t in ALL_TYPES}

_JS_PRELUDE = r"""
let rawData = { nodes: [], edges: [] };
let showFullGraph = false;
let showSuperseded = false;
let activeTypes = new Set();
let NODE_STYLE = {};
const EDGE_STYLE = { supports: { color: '#0f0', dash: '' }, contradicts: { color: '#f00', dash: '4 2' } };
let renderCalls = 0;
const renderSeen = [];
function render() {
  renderCalls += 1;
  renderSeen.push({ act: Array.from(activeTypes).sort(), dimmed: dimmedKeys() });
}
class El {
  constructor(id) {
    this.id = id; this.children = []; this.dataset = {}; this._cls = new Set();
    this.onclick = null; this._listeners = []; this._html = '';
    const self = this;
    this.classList = {
      add: c => self._cls.add(c),
      remove: c => self._cls.delete(c),
      contains: c => self._cls.has(c),
    };
  }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return Array.from(this._cls).join(' '); }
  set innerHTML(v) { this._html = v; this.children = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); return c; }
  insertAdjacentHTML() {}
  addEventListener(t, f) { this._listeners.push(f); }
  click() { if (this.onclick) this.onclick(); this._listeners.forEach(f => f()); }
}
const __els = {
  'type-filters': new El('type-filters'),
  'edge-legend': new El('edge-legend'),
  'type-hide-all': new El('type-hide-all'),
  'type-show-all': new El('type-show-all'),
};
const document = {
  getElementById: id => __els[id] || null,
  createElement: () => new El(null),
};
const tf = __els['type-filters'];
const hideBtn = __els['type-hide-all'];
const showBtn = __els['type-show-all'];
const chip = key => tf.children.find(c => c.dataset.type === key);
const dimmedKeys = () => tf.children.filter(c => c.classList.contains('dimmed')).map(c => c.dataset.type).sort();
const act = () => Array.from(activeTypes).sort();
"""


def _build_nodes(n_backbone_each: int, n_other: int):
    """Interleave backbone nodes among the others so chain edges cross types."""
    backbone = []
    for t in BACKBONE:
        for k in range(n_backbone_each):
            backbone.append(
                {
                    "id": f"{t}_{k}",
                    "type": t,
                    "created_at": f"2020-01-01T00:00:{len(backbone):02d}",
                    # one non-current backbone node, to exercise the superseded rule
                    "is_current": not (t == BACKBONE[0] and k == 0),
                }
            )
    others = []
    for i in range(n_other):
        others.append(
            {
                "id": f"{OTHER_TYPES[i % len(OTHER_TYPES)]}_{i}",
                "type": OTHER_TYPES[i % len(OTHER_TYPES)],
                "created_at": f"2026-01-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}",
                "is_current": i % 7 != 3,
            }
        )
    order = []
    bi = iter(backbone)
    for i, o in enumerate(others):
        order.append(o)
        if i % 8 == 7:
            b = next(bi, None)
            if b:
                order.append(b)
    order.extend(bi)
    return order


def _edges(nodes):
    return [
        {"source_id": nodes[i]["id"], "target_id": nodes[i + 1]["id"], "relation": "supports"}
        for i in range(len(nodes) - 1)
    ]


N_OTHER_LARGE = max(RECENT + 40, SMALL + 1)
LARGE = _build_nodes(3, N_OTHER_LARGE)
SMALL_G = _build_nodes(2, 20)
assert len(LARGE) > SMALL, "large graph must exceed LEGIBLE_SMALL_GRAPH"
assert N_OTHER_LARGE > RECENT, "need more non-backbone nodes than LEGIBLE_RECENT_LIMIT"
assert len(SMALL_G) <= SMALL, "small graph must be at most LEGIBLE_SMALL_GRAPH"
GRAPHS = {"large": LARGE, "small": SMALL_G}


def legible_oracle(nodes):
    """Python restatement of the (unchanged) legible subset rule, for the oracle."""
    if len(nodes) <= SMALL:
        return {n["id"] for n in nodes}
    back = {n["id"] for n in nodes if n["type"] in BACKBONE}
    rest = sorted(
        (n for n in nodes if n["id"] not in back),
        key=lambda n: n["created_at"],
        reverse=True,
    )[:RECENT]
    return back | {n["id"] for n in rest}


def expected(nodes, mode, active, sup):
    """C4, written down independently of graph.js."""
    leg = legible_oracle(nodes)
    ids = {
        n["id"]
        for n in nodes
        if (mode == "full" or n["id"] in leg)
        and n["type"] in active
        and (sup or n["is_current"])
    }
    edges = {
        (e["source_id"], e["target_id"])
        for e in _edges(nodes)
        if e["source_id"] in ids and e["target_id"] in ids
    }
    return ids, edges


ACTIVE_SETS = {
    "all": set(ALL_TYPES),
    "only_backbone": {BACKBONE[0]},
    "only_nonbackbone": {OTHER_TYPES[0]},
    "backbone_plus_other": {BACKBONE[1], OTHER_TYPES[1]},
    "empty": set(),
}


@unittest.skipUnless(_NODE_AVAILABLE, "node is not available on this seat")
class _NodeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._src = "\n".join(
            [
                _extract_const_line(JS_TEXT, "BACKBONE_TYPES"),
                _extract_const_line(JS_TEXT, "LEGIBLE_RECENT_LIMIT"),
                _extract_const_line(JS_TEXT, "LEGIBLE_SMALL_GRAPH"),
                _extract_function_source(JS_TEXT, "legibleNodeIds"),
                _extract_function_source(JS_TEXT, "filteredData"),
                _extract_function_source(JS_TEXT, "buildSidebar"),
            ]
        )

    def js(self, body, *, graph="large", active=None, full=False, sup=False):
        """Run ``body`` (a JS function body that ``return``s JSON-able data)
        against the real extracted functions and the stub globals."""
        nodes = GRAPHS[graph]
        act = sorted(ALL_TYPES if active is None else active)
        script = "\n".join(
            [
                _JS_PRELUDE,
                "NODE_STYLE = " + json.dumps(NODE_STYLE) + ";",
                "rawData = " + json.dumps({"nodes": nodes, "edges": _edges(nodes)}) + ";",
                "activeTypes = new Set(" + json.dumps(act) + ");",
                "showFullGraph = " + json.dumps(full) + ";",
                "showSuperseded = " + json.dumps(sup) + ";",
                self._src,
                "process.stdout.write(JSON.stringify((function(){" + body + "})()));",
            ]
        )
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        return json.loads(r.stdout)

    def filt(self, *, mode, active, graph="large", sup=False):
        out = self.js(
            "const d = filteredData();"
            "return {ids: d.nodes.map(n => n.id),"
            " types: d.nodes.map(n => n.type),"
            " edges: d.edges.map(e => [e.source, e.target]),"
            " legible: Array.from(legibleNodeIds())};",
            graph=graph,
            active=active,
            full=(mode == "full"),
            sup=sup,
        )
        return out


def _selective_guard(test, out):
    """The TRAP guard: in the large graph the legible subset must be selective."""
    all_ids = {n["id"] for n in LARGE}
    test.assertLess(len(out["legible"]), len(all_ids))
    outside = [
        n for n in LARGE if n["id"] not in set(out["legible"]) and n["type"] not in BACKBONE
    ]
    test.assertTrue(outside, "no old non-backbone node outside the legible set")


class TestFilteredDataLegible(_NodeCase):
    def test_F0_trap_guard_legible_set_is_selective(self):
        out = self.filt(mode="legible", active=ALL_TYPES)
        _selective_guard(self, out)
        # and on the small graph legible == everything (the trap itself)
        small = self.filt(mode="legible", active=ALL_TYPES, graph="small")
        self.assertEqual(len(small["legible"]), len(SMALL_G))

    def test_F1_legible_only_backbone_type_active(self):
        for t in BACKBONE:
            with self.subTest(type=t):
                out = self.filt(mode="legible", active={t}, sup=True)
                want, _ = expected(LARGE, "legible", {t}, True)
                self.assertEqual(set(out["ids"]), want)
                self.assertTrue(out["ids"])
                self.assertEqual(set(out["types"]), {t})
                self.assertLessEqual(set(out["ids"]), set(out["legible"]))

    def test_F2_legible_only_nonbackbone_active_hides_backbone(self):
        for b in BACKBONE:
            with self.subTest(hidden_backbone=b):
                active = set(ALL_TYPES) - {b}
                out = self.filt(mode="legible", active=active, sup=True)
                self.assertNotIn(b, set(out["types"]))
                want, _ = expected(LARGE, "legible", active, True)
                self.assertEqual(set(out["ids"]), want)
        out = self.filt(mode="legible", active={OTHER_TYPES[0]}, sup=True)
        self.assertTrue(out["ids"])
        self.assertEqual(set(out["types"]), {OTHER_TYPES[0]})

    def test_F3_empty_active_set_gives_nothing_in_both_modes(self):
        for mode in ("legible", "full"):
            for graph in ("large", "small"):
                for sup in (False, True):
                    with self.subTest(mode=mode, graph=graph, sup=sup):
                        out = self.filt(mode=mode, active=set(), graph=graph, sup=sup)
                        self.assertEqual(out["ids"], [])
                        self.assertEqual(out["edges"], [])

    def test_F5_legible_all_types_equals_legible_set_filtered_by_superseded(self):
        for sup in (False, True):
            with self.subTest(sup=sup):
                out = self.filt(mode="legible", active=ALL_TYPES, sup=sup)
                cur = {n["id"]: n["is_current"] for n in LARGE}
                want = {i for i in out["legible"] if sup or cur[i]}
                self.assertEqual(set(out["ids"]), want)
                # pins C5: backbone is still part of the default subset
                back = {n["id"] for n in LARGE if n["type"] in BACKBONE}
                self.assertEqual(back, back & set(out["legible"]))

    def test_F6_edges_survive_only_with_both_endpoints(self):
        base = self.filt(mode="legible", active=ALL_TYPES, sup=True)
        self.assertTrue(base["edges"], "baseline must have edges (non-vacuous)")
        for label, active in (
            ("backbone_hidden", set(OTHER_TYPES)),
            ("one_other", {OTHER_TYPES[0]}),
            ("empty", set()),
        ):
            for mode in ("legible", "full"):
                with self.subTest(case=label, mode=mode):
                    out = self.filt(mode=mode, active=active, sup=True)
                    ids = set(out["ids"])
                    for s, t in out["edges"]:
                        self.assertIn(s, ids)
                        self.assertIn(t, ids)
                    _, want = expected(LARGE, mode, active, True)
                    self.assertEqual({tuple(e) for e in out["edges"]}, want)
        hidden = self.filt(mode="legible", active=set(OTHER_TYPES), sup=True)
        self.assertLess(len(hidden["edges"]), len(base["edges"]))

    def test_F7_superseded_flag_composes_with_hidden_types(self):
        for mode in ("legible", "full"):
            for sup in (False, True):
                with self.subTest(mode=mode, sup=sup):
                    active = set(ALL_TYPES) - {BACKBONE[0], OTHER_TYPES[0]}
                    out = self.filt(mode=mode, active=active, sup=sup)
                    self.assertNotIn(BACKBONE[0], set(out["types"]))
                    self.assertNotIn(OTHER_TYPES[0], set(out["types"]))
                    want, _ = expected(LARGE, mode, active, sup)
                    self.assertEqual(set(out["ids"]), want)
                    cur = {n["id"]: n["is_current"] for n in LARGE}
                    superseded_shown = [i for i in out["ids"] if not cur[i]]
                    self.assertEqual(bool(superseded_shown), sup)

    def test_F8_full_grid_matches_oracle(self):
        for mode in ("legible", "full"):
            for graph, nodes in GRAPHS.items():
                for aname, active in ACTIVE_SETS.items():
                    for sup in (False, True):
                        with self.subTest(mode=mode, graph=graph, active=aname, sup=sup):
                            out = self.filt(mode=mode, active=active, graph=graph, sup=sup)
                            want_ids, want_edges = expected(nodes, mode, active, sup)
                            self.assertEqual(set(out["ids"]), want_ids)
                            self.assertEqual({tuple(e) for e in out["edges"]}, want_edges)


class TestFilteredDataFull(_NodeCase):
    """F4: F1/F2/F3 repeated in full mode; green before and after (pins C4's
    'full mode unchanged')."""

    def test_F4_full_mode_F1_F2_F3(self):
        for t in BACKBONE:
            with self.subTest(f1=t):
                out = self.filt(mode="full", active={t}, sup=True)
                want, _ = expected(LARGE, "full", {t}, True)
                self.assertEqual(set(out["ids"]), want)
                self.assertEqual(set(out["types"]), {t})
        for b in BACKBONE:
            with self.subTest(f2=b):
                active = set(ALL_TYPES) - {b}
                out = self.filt(mode="full", active=active, sup=True)
                self.assertNotIn(b, set(out["types"]))
        for graph in ("large", "small"):
            with self.subTest(f3=graph):
                out = self.filt(mode="full", active=set(), graph=graph)
                self.assertEqual((out["ids"], out["edges"]), ([], []))


class TestSidebar(_NodeCase):
    def test_S1_one_chip_per_node_style_key_dimmed_iff_inactive(self):
        for aname, active in ACTIVE_SETS.items():
            for calls in (1, 2):
                with self.subTest(active=aname, calls=calls):
                    out = self.js(
                        "for (let i = 0; i < " + str(calls) + "; i++) buildSidebar();"
                        "return {chips: tf.children.map(c => c.dataset.type),"
                        " dimmed: dimmedKeys()};",
                        active=active,
                    )
                    self.assertEqual(out["chips"], list(NODE_STYLE))
                    self.assertEqual(out["dimmed"], sorted(set(ALL_TYPES) - active))

    def test_S2_chip_click_toggles_exactly_that_type_no_solo(self):
        # all active: clicking one chip removes only that one (guards "solo")
        for t in ALL_TYPES:
            with self.subTest(type=t):
                out = self.js(
                    "buildSidebar(); chip('" + t + "').click();"
                    "return {act: act(), dimmed: dimmedKeys(), r: renderCalls, seen: renderSeen};"
                )
                self.assertEqual(out["act"], sorted(set(ALL_TYPES) - {t}))
                self.assertEqual(out["dimmed"], [t])
                self.assertEqual(out["r"], 1)
                self.assertEqual(
                    out["seen"], [{"act": sorted(set(ALL_TYPES) - {t}), "dimmed": [t]}]
                )
        # click a dimmed chip: it rejoins, the rest are untouched
        out = self.js(
            "buildSidebar(); chip('observation').click(); chip('observation').click();"
            "return {act: act(), dimmed: dimmedKeys(), r: renderCalls, seen: renderSeen};"
        )
        self.assertEqual(out["act"], sorted(ALL_TYPES))
        self.assertEqual(out["dimmed"], [])
        self.assertEqual(out["r"], 2)
        # first render saw observation removed, the second saw it back
        self.assertEqual(
            out["seen"],
            [
                {"act": sorted(set(ALL_TYPES) - {"observation"}), "dimmed": ["observation"]},
                {"act": sorted(ALL_TYPES), "dimmed": []},
            ],
        )
        # from a partial state, clicking a shown chip removes only it
        part = {BACKBONE[0], OTHER_TYPES[0], OTHER_TYPES[1]}
        out = self.js(
            "buildSidebar(); chip('" + OTHER_TYPES[0] + "').click();"
            "return {act: act(), r: renderCalls, seen: renderSeen};",
            active=part,
        )
        self.assertEqual(out["act"], sorted(part - {OTHER_TYPES[0]}))
        self.assertEqual(out["r"], 1)
        self.assertEqual(out["seen"][-1]["act"], sorted(part - {OTHER_TYPES[0]}))

    def test_S3_hide_all(self):
        for aname in ("all", "only_backbone", "empty"):
            with self.subTest(start=aname):
                out = self.js(
                    "buildSidebar(); hideBtn.click();"
                    "return {act: act(), dimmed: dimmedKeys(), r: renderCalls, seen: renderSeen};",
                    active=ACTIVE_SETS[aname],
                )
                self.assertEqual(out["act"], [])
                self.assertEqual(out["dimmed"], sorted(ALL_TYPES))
                self.assertEqual(out["r"], 1)
                # render saw the FINAL state, not the one before the click
                self.assertEqual(out["seen"][-1], {"act": [], "dimmed": sorted(ALL_TYPES)})

    def test_S4_show_all_from_empty_and_partial(self):
        for aname in ("empty", "only_nonbackbone", "backbone_plus_other", "all"):
            with self.subTest(start=aname):
                out = self.js(
                    "buildSidebar(); showBtn.click();"
                    "return {act: act(), dimmed: dimmedKeys(), r: renderCalls, seen: renderSeen};",
                    active=ACTIVE_SETS[aname],
                )
                self.assertEqual(out["act"], sorted(NODE_STYLE))
                self.assertEqual(out["dimmed"], [])
                self.assertEqual(out["r"], 1)
                self.assertEqual(out["seen"][-1], {"act": sorted(NODE_STYLE), "dimmed": []})

    def test_S5_hide_all_then_one_chip_leaves_exactly_that_type(self):
        for t in (BACKBONE[0], OTHER_TYPES[0]):
            with self.subTest(type=t):
                out = self.js(
                    "buildSidebar(); hideBtn.click(); chip('" + t + "').click();"
                    "return {act: act(), dimmed: dimmedKeys(), r: renderCalls, seen: renderSeen};"
                )
                self.assertEqual(out["act"], [t])
                self.assertEqual(out["dimmed"], sorted(set(ALL_TYPES) - {t}))
                self.assertEqual(out["r"], 2)
                self.assertEqual(
                    out["seen"],
                    [
                        {"act": [], "dimmed": sorted(ALL_TYPES)},
                        {"act": [t], "dimmed": sorted(set(ALL_TYPES) - {t})},
                    ],
                )

    def test_S6a_buildsidebar_does_not_create_buttons_in_type_filters(self):
        out = self.js(
            "buildSidebar(); buildSidebar();"
            "return {n: tf.children.length,"
            " has_btn: tf.children.some(c => c.id === 'type-hide-all' || c.id === 'type-show-all')};"
        )
        self.assertEqual(out["n"], len(NODE_STYLE))
        self.assertFalse(out["has_btn"])

    def test_S6b_two_buildsidebar_calls_one_click_each_one_render(self):
        for which in ("hideBtn", "showBtn"):
            with self.subTest(button=which):
                out = self.js(
                    "buildSidebar(); buildSidebar(); " + which + ".click();"
                    "return {r: renderCalls, seen: renderSeen};",
                    active={OTHER_TYPES[0]},
                )
                self.assertEqual(out["r"], 1)
                final = [] if which == "hideBtn" else sorted(NODE_STYLE)
                self.assertEqual(out["seen"][-1]["act"], final)
                self.assertEqual(
                    out["seen"][-1]["dimmed"], sorted(ALL_TYPES) if not final else []
                )


class TestMarkup(unittest.TestCase):
    def _section(self):
        parts = HTML_TEXT.split('<div class="section">')
        hits = [p for p in parts if "Node types" in p]
        self.assertEqual(len(hits), 1, "exactly one Node types section")
        return hits[0].split('<div class="section"')[0]

    def test_S6c_buttons_are_static_markup_in_node_types_section(self):
        sec = self._section()
        for bid, text in (("type-hide-all", "Hide all"), ("type-show-all", "Show all")):
            with self.subTest(button=bid):
                self.assertEqual(HTML_TEXT.count('id="' + bid + '"'), 1)
                m = re.search(r'<button[^>]*id="' + bid + r'"[^>]*>\s*([^<]*?)\s*</button>', sec)
                self.assertIsNotNone(m, bid + " must be a <button> in the Node types section")
                self.assertEqual(m.group(1), text)
                # a bare <button> inside a future <form> would submit it
                tag = re.search(r'<button[^>]*id="' + bid + r'"[^>]*>', sec).group(0)
                self.assertRegex(tag, r'\btype="button"')
        # outside #type-filters: the container is an empty element
        self.assertRegex(sec, r'<div id="type-filters">\s*</div>')


# ---------------------------------------------------------------------------
# engram-alpha#4417: the search re-enable path in searchAndFocus.
#
# Contract: searching a node whose type is hidden re-enables exactly that type
# (R1a), a node whose type is already active changes nothing (R1b), and a
# query that matches nothing changes nothing (R1c).
# Held constant (named): ORDER OF EFFECTS, state is assigned before render()
# and buildSidebar() read it, so both stubs record the sorted activeTypes AT
# CALL TIME.  Varied: TYPE (derivation vs cornerstone) x STARTING STATE (all
# hidden vs one other type active).
# STUBS: document.getElementById (a recording element), rawData, activeTypes,
# showSuperseded, showFullGraph, legibleNodeIds (holds the node id, so the
# full-graph block does nothing), buildSidebar and render (recorders),
# setTimeout.  selectNode THROWS StopAfterSelect, so the run ends before the
# zoom / svg / nodeGroup / d3 code, which these cells cannot run.
# HELD CONSTANT IN EVERY ROW, so these inputs cannot tell the real code from a
# mutant that differs only there: showSuperseded is false and is_current is
# true (the superseded block never fires, so a guard that also tests
# showSuperseded passes), the full-graph block never fires, and every type
# name is already lower case (a lower-casing of d.type passes).  A later cell
# for the superseded or full-graph block that reuses run_search must also
# revisit R1b's render == [] assertion.
# ---------------------------------------------------------------------------


@unittest.skipUnless(_NODE_AVAILABLE, "node is not available on this seat")
class TestSearchReenable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._src = _extract_function_source(JS_TEXT, "searchAndFocus")

    def run_search(self, query, *, node, active):
        script = "\n".join(
            [
                "const rawData = { nodes: [" + json.dumps(node) + "], edges: [] };",
                "const activeTypes = new Set(" + json.dumps(sorted(active)) + ");",
                "let showSuperseded = false, showFullGraph = false;",
                "const bsSeen = [], rSeen = [], selected = [];",
                "const input = { value: '', checked: false, _cls: new Set(),"
                " classList: { add: c => input._cls.add(c), remove: c => input._cls.delete(c) } };",
                "const document = { getElementById: id => input };",
                "const setTimeout = () => 0;",
                "const legibleNodeIds = () => new Set([" + json.dumps(node["id"]) + "]);",
                "const buildSidebar = () => bsSeen.push(Array.from(activeTypes).sort());",
                "const render = () => rSeen.push(Array.from(activeTypes).sort());",
                "const selectNode = d => { selected.push(d.id);"
                " throw new Error('StopAfterSelect'); };",
                self._src,
                "let stopped = false;",
                "try { searchAndFocus(" + json.dumps(query) + "); }",
                "catch (e) { if (e.message === 'StopAfterSelect') stopped = true; else throw e; }",
                "process.stdout.write(JSON.stringify({act: Array.from(activeTypes).sort(),"
                " bs: bsSeen, r: rSeen, selected, stopped,"
                " notFound: input._cls.has('not-found')}));",
            ]
        )
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        return json.loads(r.stdout)

    @staticmethod
    def _node(t):
        return {"id": t + "_target", "type": t, "is_current": True}

    def test_R1a_hidden_type_is_reenabled_before_sidebar_and_render_read_it(self):
        for t in ("derivation", "cornerstone"):
            for start_name, start in (("all_hidden", set()), ("one_other_active", {"observation"})):
                with self.subTest(type=t, start=start_name):
                    node = self._node(t)
                    out = self.run_search(node["id"].upper(), node=node, active=start)
                    want = sorted(start | {t})
                    self.assertEqual(out["act"], want)
                    self.assertEqual(out["bs"], [want])
                    self.assertEqual(out["r"], [want])
                    self.assertEqual(out["selected"], [node["id"]])
                    self.assertTrue(out["stopped"])

    def test_R1b_active_type_does_not_fire_the_reenable_block(self):
        for t in ("derivation", "cornerstone"):
            with self.subTest(type=t):
                node = self._node(t)
                start = {t, "observation"}
                out = self.run_search(node["id"], node=node, active=start)
                self.assertEqual(out["act"], sorted(start))
                self.assertEqual(out["bs"], [])
                self.assertEqual(out["r"], [])
                self.assertEqual(out["selected"], [node["id"]])
                self.assertTrue(out["stopped"])

    def test_R1c_not_found_changes_nothing(self):
        for t, start in (("derivation", set()), ("cornerstone", {"observation"})):
            with self.subTest(type=t):
                node = self._node(t)
                out = self.run_search("no_such_node", node=node, active=start)
                self.assertTrue(out["notFound"])
                self.assertEqual(out["act"], sorted(start))
                self.assertEqual(out["bs"], [])
                self.assertEqual(out["r"], [])
                self.assertEqual(out["selected"], [])
                self.assertFalse(out["stopped"])


if __name__ == "__main__":
    unittest.main()
