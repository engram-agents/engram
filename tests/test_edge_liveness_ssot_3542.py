"""Drift guard: ONE definition of which edges count as DAG structure (#3542).

Lei, 2026-10-21: "we definitely need a SSoT for this whole mechanism, and our only
maintain ONE definition, no drift ever." The definition is
``engram_core.dag_edge_set`` (built on ``_CURRENT_EDGE_PREDICATE``, itself built on
the one node-liveness predicate ``_LIVE_NODE_PREDICATE``). Every cycle judge and
walker over DAG/cascade edges calls it; none restates the endpoint filter.
Convention cannot hold that, so this sweep does, in the shape of
test_json_extract_metadata_guard_3485.py / test_no_retired_cascade_store_asserts.py.

What the scan ranges over: every ``.py`` under ``src/engram/`` (tests are not under
it). The repo root is resolved from THIS file's location.

What counts as a site (one string literal each):
  K1  a SQL literal over ``edges`` (``FROM edges`` / ``JOIN edges``) that ALSO
      tests ``<alias>.is_current`` -- an edge-endpoint liveness filter built by
      hand;
  K2  a SQL literal over ``edges`` in a function that also references a
      module-level constant whose VALUE intersects the DAG / cascade relation
      values (``CASCADE_RELATIONS``, ``DAG_RELATIONS``, ``CITATION_SUPPORT_
      RELATIONS``, their ``_SORTED`` forms, and any constant added later --
      found by VALUE at test time, not by a fixed name list) -- a raw read of
      DAG edges that did not go through ``dag_edge_set``;
  K4  a SQL literal over ``edges`` that contains one of those relation VALUES as
      a quoted literal (``relation = 'generalizes'``, ``IN ('derives_from', ...)``)
      -- the values are computed from the live constants at test time, so this
      is default-deny: a reader that never names a constant is still caught;
  K3  any reference to ``_CURRENT_EDGE_PREDICATE``
      outside its definition and ``dag_edge_set`` -- a second place applying the
      liveness predicate to edges.
Each is reported once per literal / name, innermost enclosing function.

What it CANNOT catch (so green is not "the class is closed"):
  * a relation set that reaches the SQL only through a call return or a
    parameter, and is never quoted or named (as a constant) in the reading
    function -- K2 and K4 are per function / per literal, not dataflow;
  * a liveness filter done in Python after a raw edge read (e.g. the undirected
    connectivity metric in engram_stats keeps ``current_ids`` and filters rows in
    a loop) -- no SQL literal carries it;
  * provenance-root walks over ``PROVENANCE_RELATIONS`` (a different relation set
    from the acyclicity families; not scanned);
  * SQL assembled from pieces split across literals.

Ratchet. Each allow-list entry is ``(relative path, enclosing function) ->
(exact site count, reason)``:
  * a site not covered by an entry fails (new drift);
  * an entry whose count no longer matches the tree fails the stale-entry test
    (an extra site inside an allow-listed function fails too, so an entry cannot
    hide a new one);
  * ``_MAX_ALLOWED_SITES`` is the total the allow-list may excuse: lower it,
    never raise it.
"""
from __future__ import annotations

import ast
import functools
import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "engram"

_EDGE_SQL = re.compile(r"\b(?:FROM|JOIN)\s+edges\b", re.I)
_ENDPOINT_LIVE = re.compile(r"\b\w+\.is_current\b", re.I)
_DAG_NAMES = frozenset({
    "CASCADE_RELATIONS", "_CASCADE_RELATIONS_SORTED", "_CASCADE_RELATION_PLACEHOLDERS",
    "DAG_RELATIONS", "DAG_FAMILIES", "dag_family_relations",
})
_PREDICATE_NAMES = frozenset({"_CURRENT_EDGE_PREDICATE"})

# The six module-level SQL templates behind dag_edge_set (live_only x filter).
_DAG_EDGE_TEMPLATE_NAMES = frozenset({
    "_DAG_EDGES_LIVE_ALL_SQL", "_DAG_EDGES_LIVE_SOURCE_SQL", "_DAG_EDGES_LIVE_TARGET_SQL",
    "_DAG_EDGES_ALL_STORED_ALL_SQL", "_DAG_EDGES_ALL_STORED_SOURCE_SQL",
    "_DAG_EDGES_ALL_STORED_TARGET_SQL",
})

_FAMILY_MODULES = (
    "engram_core", "engram_revision", "engram_cornerstone", "engram_epistemic",
    "engram_query", "engram_lifecycle", "engram_stats", "engram_conformance",
    "engram_dag_conformance", "engram_derive", "engram_observation", "engram_trust",
    "engram_filter",
)


@functools.lru_cache(maxsize=None)
def _live_relation_values():
    """(dag_values, const_names): COMPUTED AT TEST TIME from the live constants,
    so a relation added to the families is covered with no edit here.

    dag_values: every relation in DAG_RELATIONS, CASCADE_RELATIONS and each
    family set. const_names: the name of every module-level constant, in any
    ``engram_*`` family module, whose VALUE (a set / list / tuple of strings, or
    a single string) intersects dag_values -- ``CITATION_SUPPORT_RELATIONS`` and
    its ``_SORTED`` form included, however a future constant is named."""
    import importlib
    core = importlib.import_module("engram_core")
    values = set(core.DAG_RELATIONS) | set(core.CASCADE_RELATIONS)
    for fam in core.DAG_FAMILIES:
        values |= set(core.dag_family_relations(fam))
    names = set()
    for modname in _FAMILY_MODULES:
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        for name, val in vars(mod).items():
            if name.startswith("__") or name == "ALLOW_LIST":
                continue
            if isinstance(val, str):
                members = {val}
            elif isinstance(val, (set, frozenset, list, tuple)) and val and all(
                    isinstance(x, str) for x in val):
                members = set(val)
            else:
                continue
            if members & values:
                names.add(name)
    return frozenset(values), frozenset(names)


def _value_literal_re(values):
    return re.compile(r"""['"](?:%s)['"]""" % "|".join(sorted(map(re.escape, values))))

# The one definition (and the helper that applies it): exempt from K3 / K2 by
# construction -- not by allow-list -- so the allow-list holds only readers.
_DEFINITION_FUNCS = {
    ("engram_core.py", "dag_edge_set"),
    ("engram_core.py", "_edge_is_current"),
    ("engram_core.py", "_is_live_node"),
}

_D = "single-hop display/query read of an edge neighbourhood; not a cycle judge, walk, or DAG reachability"
_N = "one-hop live-support / existence count over non-DAG relations; not a walk"
_P = "routes its cascade hop through dag_edge_set; the remaining raw edge reads are other relations (resolves/about/supported_by/predicted_by/cites), not DAG walks"

# (relative path, enclosing function) -> (exact site count, reason)
ALLOW_LIST: dict = {
    ('engram_conformance.py', '_invariant_4'): (1, 'partition-conformance invariant: existence of a live predicted_by pairing for a prediction; not an acyclicity judge or a walk'),
    ('engram_core.py', '_backfill_edit_history'): (1, 'one-time migration backfill over supersedes edges (history, not structure)'),
    ('engram_core.py', '_count_live_exemplars'): (1, _N),
    ('engram_core.py', '_generate_snapshot'): (9, 'snapshot rendering: one-hop display reads per node (premises, contradicts, tensions, serves, exemplifies, cites/about); no walk, no cycle judgement'),
    ('engram_core.py', '_open_contradiction_ids'): (1, 'one-hop read over the contradicts relation (not an acyclicity relation)'),
    ('engram_core.py', '_rebuild_cornerstone_anchors_cache'): (1, 'derived-cache rebuild: one-hop listing of live neighbours; not a cycle judge or a walk'),
    ('engram_core.py', '_rebuild_incidents_cache'): (1, 'derived-cache rebuild: one-hop listing of live neighbours; not a cycle judge or a walk'),
    ('engram_core.py', '_rebuild_principle_triggers'): (2, 'derived-cache rebuild: one-hop listing of live neighbours; not a cycle judge or a walk'),
    ('engram_core.py', '_run_db_one_time_setup'): (1, 'startup consistency scan over stored edges and creation order (the timestamp proxy of #2357), not the live-structure gate'),
    ('engram_core.py', 'prediction_pairing_exemption_sql'): (1, 'SQL fragment for citation coverage over predicted_by pairing; not a walk or judge'),
    ('engram_cornerstone.py', '_add_edge_impl'): (1, 'point-existence check of one named edge triple (edge CRUD)'),
    ('engram_cornerstone.py', '_outgrow_cornerstone_impl'): (3, "carries a retired cornerstone's supported_by / generalizes edges to its successor (a migration of stored edges, subject is the retired node)"),
    ('engram_cornerstone.py', '_remove_edge_impl'): (2, 'point read and delete of one named edge triple (edge CRUD)'),
    ('engram_cornerstone.py', '_retype_edge_impl'): (3, 'point-existence checks of one named edge triple (edge CRUD); K2 trips only because the same function reads the cascade premise count, which goes through dag_edge_set'),
    ('engram_epistemic.py', '_scan_emergence_impl'): (1, 'one-hop read over the about relation (not an acyclicity relation)'),
    ('engram_lifecycle.py', '_reflect_impl'): (5, 'one-hop live-support / existence count over non-DAG relations; not a walk'),
    ('engram_query.py', '_build_topology_entries'): (1, _D),
    ('engram_query.py', '_get_neighbors'): (1, _D),
    ('engram_query.py', '_get_neighbors_enriched'): (1, _D),
    ('engram_query.py', '_list_impl'): (2, 'bears_on roll-up counts for list ordering (bears_on is not an acyclicity relation)'),
    ('engram_query.py', '_pattern_cornerstone_candidate'): (1, 'surfacing pattern: one-hop cite counts; not a cycle judge or a walk'),
    ('engram_query.py', '_pattern_recent_resolution_echo'): (1, 'surfacing pattern over the resolves relation (not an acyclicity relation)'),
    ('engram_query.py', '_visible_protection_channels'): (2, _N),
    ('engram_revision.py', '_bears_on_replacement_hints'): (1, 'one-hop read over bears_on (not an acyclicity relation)'),
    ('engram_revision.py', '_bears_on_resolve_fields'): (1, 'one-hop read over bears_on (not an acyclicity relation)'),
    ('engram_revision.py', '_cascade_record_acknowledgement_edges'): (1, 'existence check before writing one acknowledgement edge; the cycle checks on that write go through dag_edge_set'),
    ('engram_revision.py', '_detect_zero_support'): (1, 'one-hop live-support recount for a cornerstone or lesson; not a walk'),
    ('engram_revision.py', '_retract_impl'): (4, _P),
    ('engram_revision.py', '_supersede_impl'): (5, _P),
    ('engram_stats.py', '_diagnose_impl'): (9, 'diagnose metric counts (orphans, degree, uncited); the DAG audit in this function is check_dag_conformance, routed through dag_edge_set'),
    ('engram_trust.py', '_update_person_impl'): (1, 'one-hop listing of nodes that cite a person (about/cites/derives_from); not a walk'),
    ('health/engine.py', '_activity_diagnostics'): (1, 'activity count of supersedes edges since a cutoff; not a walk or judge'),
    ('tools/ack_recompute_sweep.py', 'sweep'): (1, 'one-off maintenance sweep: lists nodes holding an acknowledgement edge; not a cycle judge or a walk'),
    ('tools/cites_step3.py', '_main'): (1, 'migration tool over loose cites edges; not a cycle judge'),
    ('tools/cites_step3.py', 'label_row'): (1, 'migration tool over loose cites edges; not a cycle judge'),
    ('tools/cites_step3.py', 'labelled_in_bulk'): (1, 'migration tool over loose cites edges; not a cycle judge'),
    ('tools/cites_step3.py', 'loose_remaining'): (1, 'migration tool: lists loose cites edges with endpoint currency for its report; not a cycle judge'),
    ('tools/cites_step3.py', 'loose_rows'): (1, 'migration tool over loose cites edges; not a cycle judge'),
    ('tools/cites_step3.py', 'phase_a'): (1, 'migration tool over loose cites edges; not a cycle judge'),
    ('tools/cites_step3.py', 'phase_f'): (1, 'migration tool: lists loose cites edges with endpoint currency for its report; not a cycle judge'),
    ('tools/cites_step3.py', 'still_cites'): (1, 'migration tool over loose cites edges; not a cycle judge'),
    ('tools/migrate_prediction_pairing_edges.py', 'find_old_style_pairing_edges'): (1, 'one-off migration finder for old-style pairing edges; not a cycle judge'),
    ('tools/migrate_stranded_supported_by.py', 'find_stranded_edges'): (1, 'one-off migration finder for supported_by edges stranded on a retired person node; not a cycle judge'),
    ('tools/weakest_load_bearing.py', '<module>'): (1, 'tool query over the contradicts relation (not an acyclicity relation)'),
    ('viz/data/health.py', 'get_health_data'): (1, 'viz health metric (orphan count); not a cycle judge or a walk'),
}

# The ratchet: total sites the allow-list may excuse. Lower, never raise.
_MAX_ALLOWED_SITES = 80


# --------------------------------------------------------------------------
# the scanner (pure; parametrised so it can run on scratch trees)
# --------------------------------------------------------------------------

class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str, tree: ast.AST):
        self.rel = rel
        self.stack: list = []
        self.sites: list = []  # (rel, func, lineno, kinds)
        self._funcs: list = []
        self._values, self._const_names = _live_relation_values()
        self._value_re = _value_literal_re(self._values)
        self._docstrings = {
            id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
            and isinstance(n.body[0].value.value, str)
        }
        # the six fixed templates behind dag_edge_set are THE definition's text
        self._definition_literals = set()
        if rel == "engram_core.py":
            for n in tree.body:
                if (isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                        and n.targets[0].id in _DAG_EDGE_TEMPLATE_NAMES):
                    self._definition_literals.update(
                        id(x) for x in ast.walk(n.value) if isinstance(x, ast.Constant))
        self.visit(tree)

    def _enter(self, node):
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter

    def _func_name(self):
        for n in reversed(self.stack):
            return n.name
        return "<module>"

    def _func_names_used(self):
        scope = self.stack[-1] if self.stack else None
        names = set()
        if scope is None:
            return names
        for x in ast.walk(scope):
            if isinstance(x, ast.Name):
                names.add(x.id)
            elif isinstance(x, ast.Attribute):
                names.add(x.attr)
        return names

    def visit_Constant(self, node):
        if (self.rel, self._func_name()) in _DEFINITION_FUNCS:
            return
        if id(node) in self._docstrings or id(node) in self._definition_literals:
            return  # a docstring is prose, not SQL; the template literals ARE the definition
        if isinstance(node.value, str) and _EDGE_SQL.search(node.value):
            kinds = []
            if _ENDPOINT_LIVE.search(node.value):
                kinds.append("K1")
            if self._func_names_used() & (_DAG_NAMES | self._const_names):
                kinds.append("K2")
            if self._value_re.search(node.value):
                kinds.append("K4")
            if kinds:
                self.sites.append((self.rel, self._func_name(), node.lineno, "+".join(kinds)))

    def _k3(self, node, name):
        if name in _PREDICATE_NAMES and (self.rel, self._func_name()) not in _DEFINITION_FUNCS:
            # the module-level assignments that DEFINE the names are not uses
            self.sites.append((self.rel, self._func_name(), node.lineno, "K3"))

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self._k3(node, node.id)

    def visit_Attribute(self, node):
        self._k3(node, node.attr)
        self.generic_visit(node)


def scan_tree(root: Path) -> list:
    out = []
    for path in sorted(Path(root).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        out.extend(_Scan(rel, tree).sites)
    return out


def _group(sites):
    g: dict = {}
    for rel, fn, _ln, _k in sites:
        g[(rel, fn)] = g.get((rel, fn), 0) + 1
    return g


def _uncovered(sites, allow):
    g = _group(sites)
    return {k: n for k, n in g.items()
            if k not in allow or allow[k][0] < n}


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_no_module_builds_its_own_edge_liveness_filter_or_raw_dag_walk():
    sites = scan_tree(SRC_ROOT)
    bad = _uncovered(sites, ALLOW_LIST)
    detail = [f"{r}:{ln} in {fn} [{k}]" for r, fn, ln, k in sites if (r, fn) in bad]
    assert not bad, (
        "A second edge-liveness definition or a raw DAG-edge read appeared. "
        "Read DAG / cascade edges through engram_core.dag_edge_set (live_only=True "
        "for any cycle judge or liveness-bounded walk); if the read is genuinely not "
        "a DAG judgement or walk, add a reviewed ALLOW_LIST entry with its reason.\n  "
        + "\n  ".join(detail)
    )


def test_allow_list_has_no_stale_entries():
    g = _group(scan_tree(SRC_ROOT))
    stale = {k: (v[0], g.get(k, 0)) for k, v in ALLOW_LIST.items() if g.get(k, 0) != v[0]}
    assert not stale, f"allow-list entries out of date (declared, actual): {stale}"
    assert all(v[1].strip() for v in ALLOW_LIST.values()), "every entry needs a reason"


def test_allow_list_ratchet_does_not_grow():
    total = sum(v[0] for v in ALLOW_LIST.values())
    assert total <= _MAX_ALLOWED_SITES, (total, _MAX_ALLOWED_SITES)


def test_the_one_definition_is_present_and_the_gate_uses_it():
    import engram_core as core
    assert callable(core.dag_edge_set)
    assert core._LIVE_NODE_PREDICATE.format(a="x") in core._CURRENT_EDGE_PREDICATE.format(s="x", t="y")
    src = (SRC_ROOT / "engram_dag_conformance.py").read_text()
    assert "core.dag_edge_set(" in src


# -- C7: the guard goes red on a planted second definition / raw walk --------

def _plant(tmp_path, body, name="engram_planted.py"):
    root = tmp_path / "engram"
    root.mkdir()
    for p in SRC_ROOT.glob("engram_core.py"):
        shutil.copy(p, root / p.name)
    (root / name).write_text(body)
    return root


def test_c7_planted_second_endpoint_filter_is_flagged(tmp_path):
    root = _plant(tmp_path, (
        "def my_live_edges(conn):\n"
        "    return conn.execute(\n"
        "        'SELECT e.source_id FROM edges e JOIN nodes s ON s.id = e.source_id '\n"
        "        'WHERE s.is_current = 1').fetchall()\n"))
    bad = _uncovered(scan_tree(root), ALLOW_LIST)
    assert ("engram_planted.py", "my_live_edges") in bad, bad


def test_c7_planted_raw_dag_walk_is_flagged(tmp_path):
    root = _plant(tmp_path, (
        "import engram_core as core\n"
        "def my_walk(conn, node):\n"
        "    rels = sorted(core.CASCADE_RELATIONS)\n"
        "    return conn.execute(\n"
        "        'SELECT target_id FROM edges WHERE source_id = ?', (node,)).fetchall()\n"))
    bad = _uncovered(scan_tree(root), ALLOW_LIST)
    assert ("engram_planted.py", "my_walk") in bad, bad


def test_c7_planted_second_predicate_use_is_flagged(tmp_path):
    root = _plant(tmp_path, (
        "import engram_core as core\n"
        "def my_pred():\n"
        "    return core._CURRENT_EDGE_PREDICATE.format(s='a', t='b')\n"))
    bad = _uncovered(scan_tree(root), ALLOW_LIST)
    assert ("engram_planted.py", "my_pred") in bad, bad


def test_c7_green_neighbours_are_not_flagged(tmp_path):
    """Control: a routed read and a plain edge CRUD read are clean."""
    root = _plant(tmp_path, (
        "import engram_core as core\n"
        "def routed(conn, n):\n"
        "    return core.dag_edge_set(conn, core.CASCADE_RELATIONS, source_id=n)\n"
        "def crud(conn, s, t):\n"
        "    return conn.execute('SELECT id FROM edges WHERE source_id = ? AND target_id = ?',"
        " (s, t)).fetchone()\n"))
    bad = _uncovered(scan_tree(root), ALLOW_LIST)
    assert not any(k[0] == "engram_planted.py" for k in bad), bad


def test_c7_reintroducing_the_old_resolved_walk_into_a_real_module_is_flagged(tmp_path):
    """Stronger plant: copy the real conformance module and re-add a raw,
    liveness-filtered DAG edge read to it."""
    root = tmp_path / "engram"
    root.mkdir()
    for name in ("engram_core.py", "engram_dag_conformance.py"):
        shutil.copy(SRC_ROOT / name, root / name)
    p = root / "engram_dag_conformance.py"
    p.write_text(p.read_text() + (
        "\n\ndef _second_live_edges(conn):\n"
        "    return conn.execute(\n"
        "        'SELECT e.source_id, e.target_id FROM edges e '\n"
        "        'JOIN nodes s ON s.id = e.source_id JOIN nodes t ON t.id = e.target_id '\n"
        "        'WHERE s.is_current = 1 AND t.is_current = 1').fetchall()\n"))
    bad = _uncovered(scan_tree(root), ALLOW_LIST)
    assert ("engram_dag_conformance.py", "_second_live_edges") in bad, bad


# -- reviewer round-1 plants on #4010 (value-based, default-deny) -------------

def test_c7_plant_p1_raw_cascade_relations_read_is_flagged(tmp_path):
    root = _plant(tmp_path, (
        "import engram_core as core\n"
        "def p1(conn, n):\n"
        "    rels = sorted(core.CASCADE_RELATIONS)\n"
        "    return conn.execute('SELECT target_id FROM edges WHERE source_id = ? AND relation IN (?,?)',\n"
        "                        (n, *rels)).fetchall()\n"))
    assert ("engram_planted.py", "p1") in _uncovered(scan_tree(root), ALLOW_LIST)


def test_c7_plant_p2_read_via_citation_support_relations_is_flagged(tmp_path):
    root = _plant(tmp_path, (
        "import engram_core as core\n"
        "def p2(conn, n):\n"
        "    rels = sorted(core.CITATION_SUPPORT_RELATIONS)\n"
        "    return conn.execute('SELECT source_id FROM edges WHERE target_id = ? AND relation IN (?,?,?)',\n"
        "                        (n, *rels)).fetchall()\n"))
    assert ("engram_planted.py", "p2") in _uncovered(scan_tree(root), ALLOW_LIST)


def test_c7_plant_p4_relation_literals_are_flagged(tmp_path):
    root = _plant(tmp_path, (
        "def p4(conn, n):\n"
        "    return conn.execute(\"SELECT target_id FROM edges WHERE source_id = ? \"\n"
        "                        \"AND relation IN ('derives_from', 'supported_by')\", (n,)).fetchall()\n"))
    assert ("engram_planted.py", "p4") in _uncovered(scan_tree(root), ALLOW_LIST)


def test_c7_plant_p5_a_single_relation_literal_is_flagged(tmp_path):
    root = _plant(tmp_path, (
        "def p5(conn, n):\n"
        "    return conn.execute(\"SELECT source_id FROM edges WHERE target_id = ? \"\n"
        "                        \"AND relation = 'generalizes'\", (n,)).fetchall()\n"))
    assert ("engram_planted.py", "p5") in _uncovered(scan_tree(root), ALLOW_LIST)


def test_c7_a_read_of_a_non_dag_relation_by_literal_is_not_flagged(tmp_path):
    """Green neighbour: values are computed from the live constants, so a
    relation outside them (about) is clean."""
    import engram_core as core
    assert "about" not in core.DAG_RELATIONS | core.CASCADE_RELATIONS
    root = _plant(tmp_path, (
        "def neighbour(conn, n):\n"
        "    return conn.execute(\"SELECT source_id FROM edges WHERE target_id = ? \"\n"
        "                        \"AND relation = 'about'\", (n,)).fetchall()\n"))
    assert not any(k[0] == "engram_planted.py" for k in _uncovered(scan_tree(root), ALLOW_LIST))


def test_c7_values_come_from_the_live_constants_not_a_fixed_list():
    import engram_core as core
    values, names = _live_relation_values()
    assert values >= set(core.DAG_RELATIONS) | set(core.CASCADE_RELATIONS)
    assert {"CITATION_SUPPORT_RELATIONS", "_CITATION_SUPPORT_RELATIONS_SORTED",
            "CASCADE_RELATIONS", "DAG_RELATIONS"} <= names


# -- the six fixed templates are pinned to the one predicate (#3542, round 3) --

def _generated_template(core, live_only, filt):
    """What the old dynamic builder produced: the template text GENERATED from
    ``_CURRENT_EDGE_PREDICATE``. The module constants are literals (the read
    scanner admits nothing else); this is the pin that keeps them honest."""
    q = ("SELECT e.source_id AS source_id, e.target_id AS target_id, "
         "e.relation AS relation FROM edges e")
    if live_only:
        q += (" JOIN nodes s ON s.id = e.source_id"
              " JOIN nodes t ON t.id = e.target_id")
    q += " WHERE e.relation IN (SELECT value FROM json_each(?))"
    if live_only:
        q += " AND " + core._CURRENT_EDGE_PREDICATE.format(s="s", t="t")
    if filt == "source":
        q += " AND e.source_id = ?"
    elif filt == "target":
        q += " AND e.target_id = ?"
    return q + " ORDER BY e.source_id, e.target_id, e.relation"


_TEMPLATE_BY_KEY = {
    (True, "all"): "_DAG_EDGES_LIVE_ALL_SQL",
    (True, "source"): "_DAG_EDGES_LIVE_SOURCE_SQL",
    (True, "target"): "_DAG_EDGES_LIVE_TARGET_SQL",
    (False, "all"): "_DAG_EDGES_ALL_STORED_ALL_SQL",
    (False, "source"): "_DAG_EDGES_ALL_STORED_SOURCE_SQL",
    (False, "target"): "_DAG_EDGES_ALL_STORED_TARGET_SQL",
}


def test_each_dag_edge_template_equals_what_the_one_predicate_generates():
    import engram_core as core
    for (live, filt), name in _TEMPLATE_BY_KEY.items():
        assert getattr(core, name) == _generated_template(core, live, filt), name


def test_exactly_six_dag_edge_templates_exist_and_are_all_the_ones_used():
    import engram_core as core
    assert set(_TEMPLATE_BY_KEY.values()) == set(_DAG_EDGE_TEMPLATE_NAMES)
    present = {n for n in dir(core) if n.startswith("_DAG_EDGES_") and n.endswith("_SQL")}
    assert present == set(_DAG_EDGE_TEMPLATE_NAMES), present
    src = (SRC_ROOT / "engram_core.py").read_text()
    for name in present:
        # one definition + one execute use each
        assert src.count(f"conn.execute({name},") == 1, name


def test_the_relation_set_is_a_bound_parameter_never_string_built():
    import engram_core as core
    for name in _DAG_EDGE_TEMPLATE_NAMES:
        sql = getattr(core, name)
        assert "IN (SELECT value FROM json_each(?))" in sql
        for rel in core.DAG_RELATIONS:
            assert f"'{rel}'" not in sql, (name, rel)
