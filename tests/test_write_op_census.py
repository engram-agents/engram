"""Tests for tools/write_op_census.py — the call-path write-operation census
generator (#3095, phase-1 build unit U2 of #2652).

Contract-first (this repo's CLAUDE.md "Contract-first tests" convention, and
the issue's own instruction: "write these tests FIRST ... never against the
live tree"): written BEFORE tools/write_op_census.py's implementation exists,
against small synthetic fixture packages this file constructs itself. NEVER
run against the real src/engram tree — see docs/2652-engrammanager-design.md
§3.1/§7 and the dispatch spec's "Scope of THIS build" paragraph for why: this
build proves the WALKER correct on fixtures; enumerating engram-alpha's real
production roots/tables is a separate, later concern.

Seven contract items (verbatim from issue #3095), each with its own fixture,
plus the issue's own discrimination control. Section headers below name which
item each test covers.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "write_op_census.py"

_spec = importlib.util.spec_from_file_location("write_op_census", TOOL_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

run_census = _mod.run_census
main = _mod.main
CensusInputError = _mod.CensusInputError


# ---------------------------------------------------------------------------
# Fixture-construction helpers
# ---------------------------------------------------------------------------


def _write_pkg(tmp_path: Path, files: Dict[str, str]) -> Path:
    """Write `files` (relative-path -> source text) under tmp_path/pkg and
    return the package directory."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True)
    for relpath, content in files.items():
        full = pkg / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return pkg


def _run_main_cli(
    tmp_path: Path, package_dir: Path, roots: List[str], tables: List[str], capsys
) -> tuple:
    """Round-trip through the actual CLI (main()) rather than run_census()
    directly — used by the CLI-shape sanity tests."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"roots": roots, "tables": tables}))
    rc = main(["--package", str(package_dir), "--config", str(config_path)])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# Item 1 — walks call paths, not statements (three-level chain)
# ---------------------------------------------------------------------------


def test_item1_three_level_chain_credits_the_root_with_the_transitive_write(tmp_path):
    pkg = _write_pkg(
        tmp_path,
        {
            "root.py": (
                "def root_x():\n"
                "    helper_a()\n"
            ),
            "helper_a.py": (
                "def helper_a():\n"
                "    helper_b()\n"
            ),
            "helper_b.py": (
                "def helper_b():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["root.py:root_x"], tables=["widgets"])
    root_entry = result["per_root"]["root.py:root_x"]
    assert "helper_b.py:2" in root_entry["sites"]
    assert ["INSERT", "widgets"] in root_entry["pairs"]
    assert result["unreached_sites"] == []


# ---------------------------------------------------------------------------
# Item 2 — site-level check; a union over pairs must not hide one unreached
# site sharing a (verb, table) pair with a reached one
# ---------------------------------------------------------------------------


def test_item2_unreached_site_reported_even_though_its_pair_is_covered_elsewhere(
    tmp_path,
):
    pkg = _write_pkg(
        tmp_path,
        {
            "root2.py": (
                "def root_y():\n"
                "    reachable_helper()\n"
                "\n"
                "def reachable_helper():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (2)')\n"
                "\n"
                "def unreachable_helper():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (3)')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["root2.py:root_y"], tables=["widgets"])
    root_entry = result["per_root"]["root2.py:root_y"]
    # The pair (INSERT, widgets) IS covered by the reachable site...
    assert ["INSERT", "widgets"] in root_entry["pairs"]
    assert "root2.py:5" in root_entry["sites"]
    # ...but the OTHER site carrying the identical pair is still reported
    # unreached, specifically, by file:line — a union-over-pairs check would
    # have missed this because "INSERT/widgets" already looks covered.
    assert "root2.py:8" in result["unreached_sites"]
    assert "root2.py:8" not in root_entry["sites"]


# ---------------------------------------------------------------------------
# Item 3 — per-root mapping is the deliverable, not a codebase-wide union
# ---------------------------------------------------------------------------


def test_item3_helper_reached_from_root_a_only_is_credited_to_a_not_b(tmp_path):
    pkg = _write_pkg(
        tmp_path,
        {
            "multi.py": (
                "def root_a():\n"
                "    shared_helper()\n"
                "    only_a_helper()\n"
                "\n"
                "def root_b():\n"
                "    shared_helper()\n"
                "\n"
                "def shared_helper():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (10)')\n"
                "\n"
                "def only_a_helper():\n"
                "    cursor.execute('UPDATE widgets SET x = 1')\n"
            ),
        },
    )
    result = run_census(
        pkg, roots=["multi.py:root_a", "multi.py:root_b"], tables=["widgets"]
    )
    a_sites = set(result["per_root"]["multi.py:root_a"]["sites"])
    b_sites = set(result["per_root"]["multi.py:root_b"]["sites"])
    # shared_helper's write site: reached from BOTH roots.
    assert "multi.py:9" in a_sites
    assert "multi.py:9" in b_sites
    # only_a_helper's write site: reached from A only, must NOT be lumped
    # into a codebase-wide reachable-set that would also credit it to B.
    assert "multi.py:12" in a_sites
    assert "multi.py:12" not in b_sites
    assert result["unreached_sites"] == []


# ---------------------------------------------------------------------------
# Item 4 — a self-rooted site exists ONLY by declaration, never by fallback
# ---------------------------------------------------------------------------


def _orphan_fixture(tmp_path) -> Path:
    return _write_pkg(
        tmp_path,
        {
            "orphan.py": (
                "def unrelated_root():\n"
                "    pass\n"
                "\n"
                "def orphan_write():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (99)')\n"
            ),
        },
    )


def test_item4a_unreached_site_not_declared_as_root_stays_unreached(tmp_path):
    pkg = _orphan_fixture(tmp_path)
    result = run_census(
        pkg, roots=["orphan.py:unrelated_root"], tables=["widgets"]
    )
    assert "orphan.py:5" in result["unreached_sites"]
    for entry in result["per_root"].values():
        assert "orphan.py:5" not in entry["sites"]


def test_item4b_same_site_explicitly_declared_as_root_is_now_reached_self_credited(
    tmp_path,
):
    pkg = _orphan_fixture(tmp_path)
    result = run_census(
        pkg,
        roots=["orphan.py:unrelated_root", "orphan.py:5"],
        tables=["widgets"],
    )
    assert "orphan.py:5" not in result["unreached_sites"]
    # Credited to ITSELF — the per-root key is the declared file:line string,
    # not folded into unrelated_root or any other root.
    self_root_entry = result["per_root"]["orphan.py:5"]
    assert "orphan.py:5" in self_root_entry["sites"]
    assert "orphan.py:5" not in result["per_root"]["orphan.py:unrelated_root"]["sites"]


def test_item4c_no_code_path_auto_promotes_an_unreached_site_to_a_root(tmp_path):
    """Discipline check: declaring ZERO roots at all must still leave the
    orphan site unreached — nothing in run_census may treat "no root reaches
    this site" as license to call it its own root automatically."""
    pkg = _orphan_fixture(tmp_path)
    result = run_census(pkg, roots=[], tables=["widgets"])
    assert result["per_root"] == {}
    assert "orphan.py:5" in result["unreached_sites"]


# ---------------------------------------------------------------------------
# Item 5 — the table set is a declared input; an out-of-scope write is
# reported explicitly, never silently dropped
# ---------------------------------------------------------------------------


def test_item5_write_to_undeclared_table_is_reported_out_of_scope(tmp_path):
    pkg = _write_pkg(
        tmp_path,
        {
            "scope.py": (
                "def root_z():\n"
                "    cursor.execute('DELETE FROM vec_nodes WHERE id = 1')\n"
            ),
        },
    )
    # vec_nodes deliberately excluded from the declared table set, mirroring
    # §3.1's real vec_nodes virtual-table wrinkle.
    result = run_census(pkg, roots=["scope.py:root_z"], tables=["widgets"])
    out_of_scope = result["out_of_scope"]
    assert {"site": "scope.py:2", "verb": "DELETE", "table": "vec_nodes"} in out_of_scope
    # Must not be silently folded into the root's in-scope pairs.
    assert ["DELETE", "vec_nodes"] not in result["per_root"]["scope.py:root_z"]["pairs"]


def test_item5_out_of_scope_write_reported_even_when_unreached(tmp_path):
    """The out-of-scope finding must not depend on reachability — an
    out-of-scope site that NO root reaches must still surface, exactly as an
    in-scope unreached site would."""
    pkg = _write_pkg(
        tmp_path,
        {
            "scope2.py": (
                "def unrelated():\n"
                "    pass\n"
                "\n"
                "def never_called():\n"
                "    cursor.execute('DELETE FROM vec_nodes WHERE id = 1')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["scope2.py:unrelated"], tables=["widgets"])
    assert {"site": "scope2.py:5", "verb": "DELETE", "table": "vec_nodes"} in result[
        "out_of_scope"
    ]
    assert "scope2.py:5" in result["unreached_sites"]


# ---------------------------------------------------------------------------
# Item 6 — one statement, two verbs: INSERT ... ON CONFLICT ... DO UPDATE
# ---------------------------------------------------------------------------


def test_item6_insert_on_conflict_do_update_recorded_as_named_composite_verb(
    tmp_path,
):
    """Documented rule (see module docstring): an INSERT ... ON CONFLICT ...
    DO UPDATE statement is recorded as ONE site carrying the named composite
    verb "upsert" against the target table — not two separate (INSERT,
    table) and (UPDATE, table) pairs."""
    pkg = _write_pkg(
        tmp_path,
        {
            "upsert.py": (
                "def root_u():\n"
                "    conn.execute(\n"
                "        'INSERT INTO widgets (id) VALUES (1) '\n"
                "        'ON CONFLICT(id) DO UPDATE SET id = 1'\n"
                "    )\n"
            ),
        },
    )
    result = run_census(pkg, roots=["upsert.py:root_u"], tables=["widgets"])
    pairs = result["per_root"]["upsert.py:root_u"]["pairs"]
    assert pairs == [["upsert", "widgets"]]
    assert ["INSERT", "widgets"] not in pairs
    assert ["UPDATE", "widgets"] not in pairs


# ---------------------------------------------------------------------------
# Item 7 — known holes printed in the output header
# ---------------------------------------------------------------------------


def test_item7_output_header_names_all_four_known_holes(tmp_path):
    pkg = _write_pkg(
        tmp_path,
        {"trivial.py": "def root():\n    pass\n"},
    )
    result = run_census(pkg, roots=["trivial.py:root"], tables=[])
    limitations = result["known_limitations"]
    assert len(limitations) == 4
    assert any(line.startswith("(a)") for line in limitations)
    assert any(line.startswith("(b)") for line in limitations)
    assert any(line.startswith("(c)") for line in limitations)
    assert any(line.startswith("(d)") for line in limitations)
    # (a): existential-over-roots vs. the per-root mapping deliverable.
    assert any("per-root" in line.lower() or "per root" in line.lower() for line in limitations)
    # (b): the root set is prescribed, not checked.
    assert any("prescribed" in line.lower() for line in limitations)
    # (c): this build's own scope statement — .py files only.
    assert any(".py" in line for line in limitations)
    # (d): the declared table set is declared, not derived.
    assert any("declared, not derived" in line.lower() for line in limitations)


# ---------------------------------------------------------------------------
# Discrimination control (from the issue): delete one call edge and confirm
# item 2's unreached-site check goes RED for the newly-orphaned site while
# an unrelated chain stays GREEN.
# ---------------------------------------------------------------------------


def _chain_fixture(tmp_path, *, break_edge: bool) -> Path:
    helper_a_body = (
        "def helper_a():\n"
        + ("    pass\n" if break_edge else "    helper_b()\n")
    )
    return _write_pkg(
        tmp_path,
        {
            "root.py": (
                "def root_x():\n"
                "    helper_a()\n"
                "\n"
                "def root_c():\n"
                "    helper_c()\n"
            ),
            "helper_a.py": helper_a_body,
            "helper_b.py": (
                "def helper_b():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)')\n"
            ),
            "helper_c.py": (
                "def helper_c():\n"
                "    cursor.execute('INSERT INTO gizmos (id) VALUES (1)')\n"
            ),
        },
    )


def test_discrimination_control_deleting_an_edge_reddens_only_the_touched_site(
    tmp_path,
):
    tables = ["widgets", "gizmos"]
    roots = ["root.py:root_x", "root.py:root_c"]

    intact_pkg = _chain_fixture(tmp_path / "intact", break_edge=False)
    baseline = run_census(intact_pkg, roots=roots, tables=tables)
    assert baseline["unreached_sites"] == []
    assert "helper_b.py:2" in baseline["per_root"]["root.py:root_x"]["sites"]
    assert "helper_c.py:2" in baseline["per_root"]["root.py:root_c"]["sites"]

    broken_pkg = _chain_fixture(tmp_path / "broken", break_edge=True)
    mutated = run_census(broken_pkg, roots=roots, tables=tables)
    # RED: helper_b's write, reachable only via the now-deleted edge, becomes
    # unreached.
    assert "helper_b.py:2" in mutated["unreached_sites"]
    assert "helper_b.py:2" not in mutated["per_root"]["root.py:root_x"]["sites"]
    # GREEN: the untouched chain (root_c -> helper_c) is unaffected.
    assert "helper_c.py:2" in mutated["per_root"]["root.py:root_c"]["sites"]
    assert "helper_c.py:2" not in mutated["unreached_sites"]


# ---------------------------------------------------------------------------
# Item 8 (round-2 fix) — two write sites sharing one physical line must NOT
# collide: before the fix, `(file, line)`-only keying in site_by_loc /
# _bfs_reach's `reached` / run_census's `combined` silently dropped the
# first of two same-line sites from every output section alike (sites,
# pairs, unreached_sites, out_of_scope). Fixed by keying on
# `(file, line, col_offset)` internally while keeping the DISPLAYED site id
# as `file:line` (see Site.id's docstring) -- so a genuine same-line
# collision surfaces as the SAME id string appearing twice, not as one site
# vanishing.
# ---------------------------------------------------------------------------


def test_item8a_two_writes_on_one_physical_line_both_survive(tmp_path):
    """Semicolon-chained statements are a genuinely valid Python source
    shape producing two `ast.Call` write-sites at the same `lineno` but
    different `col_offset`s -- before the fix, only the SECOND (DELETE)
    site would survive in every output section."""
    pkg = _write_pkg(
        tmp_path,
        {
            "collision.py": (
                "def root_w():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)'); "
                "cursor.execute('DELETE FROM widgets WHERE id = 1')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["collision.py:root_w"], tables=["widgets"])
    root_entry = result["per_root"]["collision.py:root_w"]
    # Both sites share the SAME displayed id string ("collision.py:2") --
    # the fix does not change the id format (see Site.id's docstring) -- so
    # the discriminating check is that the id appears TWICE (once per
    # surviving Site), not merely that it appears (a single surviving site
    # would also pass a plain `in` membership check).
    assert root_entry["sites"].count("collision.py:2") == 2
    assert ["INSERT", "widgets"] in root_entry["pairs"]
    assert ["DELETE", "widgets"] in root_entry["pairs"]
    assert result["unreached_sites"] == []


def test_item8b_same_line_collision_also_preserved_in_unreached_and_out_of_scope(
    tmp_path,
):
    """The reviewer's own finding named FOUR affected surfaces -- sites,
    pairs, unreached_sites, AND out_of_scope. Item 8a covers the first two
    (reached-and-credited); this covers the other two, using a write site
    that is neither reached by any root nor in the declared table set."""
    pkg = _write_pkg(
        tmp_path,
        {
            "collision2.py": (
                "def unrelated():\n"
                "    pass\n"
                "\n"
                "def never_called():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)'); "
                "cursor.execute('DELETE FROM gizmos WHERE id = 1')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["collision2.py:unrelated"], tables=["widgets"])
    # Both same-line sites are unreached (root_w never calls never_called) --
    # before the fix, site_by_loc would hold only ONE of the two Site
    # objects (the second .execute() call overwriting the first), so
    # all_sites (derived from site_by_loc) would yield this id only once.
    assert result["unreached_sites"].count("collision2.py:5") == 2
    # The gizmos write (undeclared table) must surface in the GLOBAL
    # out_of_scope list -- this ONLY happens if its Site object survived in
    # site_by_loc alongside the widgets write sharing its line.
    assert any(
        d["site"] == "collision2.py:5" and d["verb"] == "DELETE" and d["table"] == "gizmos"
        for d in result["out_of_scope"]
    )
    # The widgets write (declared table) must NOT appear in out_of_scope --
    # confirming the two same-line Site objects are tracked independently,
    # not merged/confused with each other.
    assert not any(
        d["site"] == "collision2.py:5" and d["table"] == "widgets"
        for d in result["out_of_scope"]
    )


# ---------------------------------------------------------------------------
# Item 9 (round-2 fix) — the two already-documented-but-untested design
# rules the reviewer named: bare-name cross-package call resolution credits
# EVERY name match, and attribute-form calls are excluded from edge
# resolution entirely.
# ---------------------------------------------------------------------------


def test_item9a_bare_name_collision_credits_every_matching_function(tmp_path):
    """Documented rule (module docstring's "Call-graph construction"): if
    more than one function shares a bare-called name across the package,
    edges are added to EVERY match -- a deliberate over-approximation. Two
    same-named `helper()` functions in different files, both reachable from
    one bare `helper()` call, must BOTH have their write sites credited to
    the calling root."""
    pkg = _write_pkg(
        tmp_path,
        {
            "root9.py": (
                "def root_h():\n"
                "    helper()\n"
            ),
            "helperA.py": (
                "def helper():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)')\n"
            ),
            "helperB.py": (
                "def helper():\n"
                "    cursor.execute('DELETE FROM widgets WHERE id = 1')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["root9.py:root_h"], tables=["widgets"])
    sites = result["per_root"]["root9.py:root_h"]["sites"]
    pairs = result["per_root"]["root9.py:root_h"]["pairs"]
    # BOTH same-named helper()s' write sites are credited -- not just
    # whichever one happened to be resolved first.
    assert "helperA.py:2" in sites
    assert "helperB.py:2" in sites
    assert ["INSERT", "widgets"] in pairs
    assert ["DELETE", "widgets"] in pairs
    assert result["unreached_sites"] == []


def test_item9b_attribute_form_call_is_not_resolved_as_an_edge(tmp_path):
    """Documented rule (module docstring's "Call-graph construction"):
    attribute-form calls (`self.helper()`, `obj.method()`) are deliberately
    NOT resolved as call-graph edges -- only bare `name(...)` calls are. A
    method that reaches its sibling only via `self.helper()` must NOT have
    that sibling's write site credited to it."""
    pkg = _write_pkg(
        tmp_path,
        {
            "attr.py": (
                "class Foo:\n"
                "    def root_m(self):\n"
                "        self.helper()\n"
                "\n"
                "    def helper(self):\n"
                "        cursor.execute('INSERT INTO widgets (id) VALUES (1)')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["attr.py:root_m"], tables=["widgets"])
    assert "attr.py:6" not in result["per_root"]["attr.py:root_m"]["sites"]
    # Not reached by any other root either -- genuinely unreached, not just
    # absent from this one root's mapping.
    assert "attr.py:6" in result["unreached_sites"]


# ---------------------------------------------------------------------------
# CLI-shape sanity (not one of the 7 contract items, but the tool must have a
# working CLI entry point per the spec's "Output format" design choice)
# ---------------------------------------------------------------------------


def test_cli_round_trip_prints_valid_json_with_the_header(tmp_path, capsys):
    pkg = _write_pkg(
        tmp_path,
        {
            "root.py": (
                "def root_x():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)')\n"
            ),
        },
    )
    rc, out, err = _run_main_cli(tmp_path, pkg, ["root.py:root_x"], ["widgets"], capsys)
    assert rc == 0, f"stderr={err!r}"
    parsed = json.loads(out)
    assert len(parsed["known_limitations"]) == 4
    assert ["INSERT", "widgets"] in parsed["per_root"]["root.py:root_x"]["pairs"]


def test_cli_refuses_loudly_on_unresolvable_root(tmp_path, capsys):
    pkg = _write_pkg(tmp_path, {"root.py": "def root_x():\n    pass\n"})
    rc, out, err = _run_main_cli(
        tmp_path, pkg, ["root.py:does_not_exist"], ["widgets"], capsys
    )
    assert rc != 0
    assert "does_not_exist" in err


def test_run_census_raises_census_input_error_on_unresolvable_root(tmp_path):
    pkg = _write_pkg(tmp_path, {"root.py": "def root_x():\n    pass\n"})
    with pytest.raises(CensusInputError):
        run_census(pkg, roots=["root.py:does_not_exist"], tables=["widgets"])


# ---------------------------------------------------------------------------
# Item 10 (round-2 fix) — the 3 untested CLI exit-2 error paths. The
# docstring's own CLI contract (see module docstring's "CLI" section) names
# four distinct exit-2 conditions; only "unresolvable root" (above) had a
# fixture test before this round. These four tests cover the rest.
# ---------------------------------------------------------------------------


def test_item10a_cli_refuses_loudly_on_missing_package_directory(tmp_path, capsys):
    missing_pkg = tmp_path / "does_not_exist_pkg"
    rc, out, err = _run_main_cli(tmp_path, missing_pkg, [], [], capsys)
    assert rc == 2
    assert out == ""
    assert "package directory not found" in err
    assert str(missing_pkg) in err


def test_item10b_cli_refuses_loudly_on_unreadable_config_file(tmp_path, capsys):
    """"Unreadable" per the docstring's own CLI contract -- exercised here
    via a config PATH that is a directory rather than a file (portable
    across platforms/UIDs, unlike a permission-bit test), which trips the
    same `read_text()` `OSError` branch main() catches (IsADirectoryError is
    an OSError subclass)."""
    pkg = _write_pkg(tmp_path, {"root.py": "def root_x():\n    pass\n"})
    config_as_dir = tmp_path / "config_is_a_dir.json"
    config_as_dir.mkdir()
    rc = main(["--package", str(pkg), "--config", str(config_as_dir)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "cannot read config" in captured.err


def test_item10c_cli_refuses_loudly_on_malformed_json_config(tmp_path, capsys):
    pkg = _write_pkg(tmp_path, {"root.py": "def root_x():\n    pass\n"})
    bad_config = tmp_path / "bad_config.json"
    bad_config.write_text("{not valid json")
    rc = main(["--package", str(pkg), "--config", str(bad_config)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "malformed JSON config" in captured.err


def test_item11_sites_and_pairs_are_returned_in_literal_deterministic_sort_order(
    tmp_path,
):
    """Optional (spec item 6): every existing assertion up to this point
    checks set/membership only, never literal list order, even though
    deterministic sort order is documented (`run_census`'s `sorted(...,
    key=lambda s: (s.file, s.line, s.col_offset))` for sites/out_of_scope,
    `sorted({...})` for pairs). Call order here (helper_z() before
    helper_a()) deliberately DISAGREES with alphabetical file order, so a
    correct implementation's re-sort is what the exact-equality assertions
    below are actually exercising -- not an accident of BFS traversal
    order."""
    pkg = _write_pkg(
        tmp_path,
        {
            "root10.py": (
                "def root_j():\n"
                "    helper_z()\n"
                "    helper_a()\n"
            ),
            "helper_z.py": (
                "def helper_z():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)')\n"
            ),
            "helper_a.py": (
                "def helper_a():\n"
                "    cursor.execute('DELETE FROM widgets WHERE id = 1')\n"
            ),
        },
    )
    result = run_census(pkg, roots=["root10.py:root_j"], tables=["widgets"])
    root_entry = result["per_root"]["root10.py:root_j"]
    # sites: sorted by (file, line) -- "helper_a.py" before "helper_z.py",
    # the OPPOSITE of call order.
    assert root_entry["sites"] == ["helper_a.py:2", "helper_z.py:2"]
    # pairs: sorted by (verb, table) -- "DELETE" before "INSERT".
    assert root_entry["pairs"] == [["DELETE", "widgets"], ["INSERT", "widgets"]]


def test_item10d_cli_refuses_loudly_when_roots_or_tables_present_but_not_lists(
    tmp_path, capsys
):
    """Precise reading of the docstring's "a config missing the `roots`/
    `tables` list keys" exit-2 condition, checked against the ACTUAL
    implementation (`main()`'s `isinstance(roots, list)` / `isinstance(
    tables, list)` guard): a config with the keys entirely ABSENT does NOT
    hit this guard -- `config.get("roots", [])` / `config.get("tables", [])`
    silently default to `[]`, which passes the isinstance check, so `main()`
    returns 0 with an empty-roots result rather than exit 2 (verified
    directly: flagged as a real docstring/implementation discrepancy in this
    round's handoff, not silently reconciled either way -- out of this
    round's scope to fix). This test instead covers what the guard ACTUALLY
    catches, per the spec's own "(or present but not lists)" alternative
    phrasing: the keys present with a non-list value."""
    pkg = _write_pkg(tmp_path, {"root.py": "def root_x():\n    pass\n"})
    bad_config = tmp_path / "not_a_list_config.json"
    bad_config.write_text(json.dumps({"roots": "root.py:root_x", "tables": ["widgets"]}))
    rc = main(["--package", str(pkg), "--config", str(bad_config)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "'roots' and 'tables' as JSON lists" in captured.err


# ---------------------------------------------------------------------------
# Item 12 (round-3 hardening) — item8a/item8b's fixtures both use two
# DIFFERENT verbs (INSERT vs. DELETE) for their two colliding same-line write
# sites, so a plausible-wrong `(file, line, verb)`-keyed mutation ALSO
# disambiguates them and passes all 23 pre-round-3 tests -- verb happens to
# do the job those fixtures need, without proving the shipped `col_offset`
# keying is what's actually doing it. This fixture uses the SAME verb
# (INSERT) for both same-line sites, targeting different tables, so it
# discriminates the two keyings: `col_offset` (always distinct per AST node)
# keeps both sites; `verb` (identical here) collapses them to one.
# ---------------------------------------------------------------------------


def test_item12_two_same_verb_writes_on_one_physical_line_both_survive_with_distinct_tables(
    tmp_path,
):
    """Two semicolon-chained INSERTs into different tables on one physical
    line. Under the real `(file, line, col_offset)` keying both Site objects
    survive (col_offset always differs for two distinct AST nodes); under a
    `(file, line, verb)`-keyed mutation the two same-verb sites share a key
    and one silently overwrites the other."""
    pkg = _write_pkg(
        tmp_path,
        {
            "collision3.py": (
                "def root_w():\n"
                "    cursor.execute('INSERT INTO widgets (id) VALUES (1)'); "
                "cursor.execute('INSERT INTO gizmos (id) VALUES (1)')\n"
            ),
        },
    )
    result = run_census(
        pkg, roots=["collision3.py:root_w"], tables=["widgets", "gizmos"]
    )
    root_entry = result["per_root"]["collision3.py:root_w"]
    # Both sites share the SAME displayed id string ("collision3.py:2") and
    # the SAME verb ("INSERT") -- only col_offset (invisible in the id
    # string, per Site.id's docstring) distinguishes them internally, so the
    # discriminating check is that the id appears TWICE.
    assert root_entry["sites"].count("collision3.py:2") == 2
    assert ["INSERT", "widgets"] in root_entry["pairs"]
    assert ["INSERT", "gizmos"] in root_entry["pairs"]
    assert result["unreached_sites"] == []
