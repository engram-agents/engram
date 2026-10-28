"""engram-alpha#3983: sibling-runtime-file lookups stop at their own tree root.

THE CONTRACT (group-A sites of the issue).  A site that finds a *sibling runtime
file* shipped in its own tree searches only from the file's own directory up to
and INCLUDING its tree root (the nearest ancestor holding ``engram_core.py``:
the plugin root installed, ``src/engram/`` in a source checkout), nearest first,
and never above it.  A file not found inside that range is ABSENT (each caller
already handles absence).  With no ``engram_core.py`` anywhere above, only the
file's own directory and its parent are searched.  ``CLAUDE_PLUGIN_ROOT`` stays
the first candidate where the site already used it.

Cells, run for every site against an INSTALLED layout and a SOURCE layout built
in ``tmp_path``, executing the REAL shipped source of the site (the function
extracted from the file, or the whole hook for the import-time blocks) with
``__file__`` pointing into the fixture, in a clean subprocess:

  (a) target inside the tree: found, and the NEAREST copy wins.
  (b) DISCRIMINATING: target absent in the tree, planted one level ABOVE the
      tree root, ``CLAUDE_PLUGIN_ROOT`` unset: must NOT be found.  A clean tree
      cannot tell a bounded walk from an unbounded one.
  (c) ``CLAUDE_PLUGIN_ROOT`` set: checked first (where the site supports it).
  (d) viz/data/*: ``viz/server.py`` must not stop the walk at ``viz/``.
  (e) no ``engram_core.py`` anywhere: parent found, grandparent NOT.

Plus a drift test: every group-A source file CALLS the bounded walk where it
looks (an AST ``Call`` that is a ``for`` iterable or a ``.extend(...)`` argument;
the helper's own ``def`` line cannot satisfy it), each local definition names
``engram_core.py``, and every local copy of ``bounded_parents`` behaves
identically, including skipping an unreadable directory and walking on.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "engram"
HOOKS = SRC / "hooks" / "claude"
MARKER = "engram_core.py"

DRIVER = r'''
import ast, json, os, sys
from pathlib import Path
from typing import Optional
spec = json.loads(sys.argv[1])
fixture = spec["fixture"]
src = Path(spec["real"]).read_text()
g = {"__file__": fixture, "__name__": "site_under_test", "os": os, "sys": sys,
     "Path": Path, "Optional": Optional}
g.update(spec.get("consts", {}))
if spec.get("preamble"):
    sys.path[:0] = [spec["src"]]
    exec(spec["preamble"], g)
try:
    if spec["mode"] == "module":
        sys.path[:0] = spec.get("sys_path", [])
        exec(compile(src, fixture, "exec"), g)
        res = eval(spec["expr"], g)
    else:
        wanted = set(spec["funcs"])
        body = [n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name in wanted]
        exec(compile(ast.Module(body=body, type_ignores=[]), fixture, "exec"), g)
        args = [Path(fixture) if a == "$FILE" else a for a in spec.get("args", [])]
        res = g[spec["entry"]](*args)
except RuntimeError:
    res = None  # cites_step3 reports absence by raising RuntimeError
print("RESULT:" + json.dumps(None if res is None else str(res)))
'''


@dataclass
class Site:
    name: str
    real: str                 # path under src/engram
    kind: str                 # tools | hooks | viz | root
    mode: str                 # func | module
    plant: str                # target path relative to a candidate ancestor p
    to_file: str              # how the returned value maps to the target file
    funcs: list = field(default_factory=list)
    entry: str = ""
    args: list = field(default_factory=list)
    expr: str = ""
    consts: dict = field(default_factory=dict)
    plugin_root: bool = True          # does the site consult CLAUDE_PLUGIN_ROOT?
    marker_is_target: bool = False
    extra_env: dict = field(default_factory=dict)
    sibling_first: bool = False
    plant_alt: str = ""               # a second candidate shape under p (e.g. src/engram)
    preamble: str = ""                # run before the extracted function (an import it relies on)


_HELPER = dict(kind="tools", mode="func", plant="engram_regular_io.py",
               to_file="dir/engram_regular_io.py", funcs=["_find_helper_dir"],
               entry="_find_helper_dir", plant_alt="src/engram/engram_regular_io.py")
_LOOP = dict(kind="hooks", mode="module", plant="tools/loop_prompt.py",
             to_file="dir/loop_prompt.py", expr="_LP_TOOLS_DIR")

_OWNER_READ = "from tools import _owner_read as owner_read"
_REFUSAL = "class _Refusal(RuntimeError):\n    pass\n"  # engram_signature reports absence by raising its own _Refusal
_HOOK_RELS = ["hooks/claude/engram-lesson-tripwire-hook.py", "hooks/engram-lesson-tripwire-hook.py"]

SITES = [
    Site("_owner_read", "tools/_owner_read.py", kind="tools", mode="func",
         plant="engramd_client.py", to_file="dir/engramd_client.py",
         funcs=["runtime_dir"], entry="runtime_dir"),
    Site("engram-sign", "tools/engram-sign.py", kind="tools", mode="func",
         plant="engram_signature.py", to_file="dir/engram_signature.py",
         funcs=["_find_module_dir"], entry="_find_module_dir", preamble=_OWNER_READ),
    Site("reconcile_lesson_demotions", "tools/reconcile_lesson_demotions.py", kind="tools",
         mode="func", plant=_HOOK_RELS[0], to_file="file",
         funcs=["_resolve_hook_path"], entry="_resolve_hook_path", preamble=_OWNER_READ,
         consts={"_HOOK_RELATIVE_PATHS": _HOOK_RELS}, plant_alt=_HOOK_RELS[1]),
    # The "target" is a PAIR (plugin.json + server.py); `plant` plants both and returns plugin.json.
    Site("engram_signature", "engram_signature.py", kind="root", mode="func",
         plant="plugin.json", to_file="dir/plugin.json",
         funcs=["_find_plugin_root"], entry="_find_plugin_root",
         preamble=_REFUSAL),
    Site("check_stale_processes", "tools/check_stale_processes.py", **_HELPER),
    Site("monitor-supervisor", "tools/monitor-supervisor.py", **_HELPER),
    Site("codex-umwelt-wake", "tools/codex-umwelt-wake.py", **_HELPER),
    Site("cites_step3", "tools/cites_step3.py", kind="tools", mode="func",
         plant="engram_core.py", to_file="dir/engram_core.py",
         funcs=["_find_engine_dir"], entry="_find_engine_dir",
         marker_is_target=True, plant_alt="src/engram/engram_core.py"),
    Site("engramd_owner", "engramd_owner.py", kind="root", mode="func",
         plant="engramd_read_templates.json", to_file="file",
         funcs=["_find_read_templates_path"], entry="_find_read_templates_path",
         consts={"READ_TEMPLATES_FILENAME": "engramd_read_templates.json"},
         sibling_first=True),
    Site("_hooklib.resolve_tools_dir", "hooks/claude/_hooklib.py", kind="hooks",
         mode="func", plant="tools/x_marker.py", to_file="dir/x_marker.py",
         funcs=["resolve_tools_dir"], entry="resolve_tools_dir",
         args=["x_marker.py"]),
    Site("engram-deference-detector-prompt", "hooks/claude/engram-deference-detector-prompt.py",
         **_LOOP),
    Site("engram-time-bar-hook", "hooks/claude/engram-time-bar-hook.py", **_LOOP),
    Site("engram-loop-arm-stop-hook", "hooks/claude/engram-loop-arm-stop-hook.py",
         **_LOOP),
    Site("engram-surface-hook", "hooks/claude/engram-surface-hook.py", kind="hooks",
         mode="module", plant="engram_search_rules.py", to_file="file",
         expr="(_not_search_excluded_sql.__code__.co_filename "
              "if _not_search_excluded_sql else None)",
         plugin_root=False, plant_alt="src/engram/engram_search_rules.py"),
    Site("viz/data/stats", "viz/data/stats.py", kind="viz", mode="func",
         plant="engram_log_indexer.py", to_file="dir/engram_log_indexer.py",
         funcs=["_find_indexer_dir"], entry="_find_indexer_dir", args=["$FILE"],
         plugin_root=False),
    Site("viz/data/config", "viz/data/config.py", kind="viz", mode="func",
         plant="tools/config_schema.py", to_file="dir/tools/config_schema.py",
         funcs=["_find_tools_parent"], entry="_find_tools_parent", args=["$FILE"],
         plugin_root=False),
]
SITE_IDS = [s.name for s in SITES]


@dataclass
class Layout:
    base: Path
    root: Path      # the tree root (engram_core.py lives here)
    above: Path     # one level ABOVE the tree root
    file_dir: Path  # directory the site's file lives in


def build_layout(tmp: Path, flavour: str, site: Site, with_marker=True) -> Layout:
    if flavour == "installed":
        base = tmp / "stale_home"
        root = base / "plugin"
        subs = {"tools": "tools", "hooks": "hooks", "viz": "viz/data", "root": ""}
    else:
        base = tmp / "repo"
        root = base / "src" / "engram"
        subs = {"tools": "tools", "hooks": "hooks/claude", "viz": "viz/data", "root": ""}
    file_dir = root / subs[site.kind] if subs[site.kind] else root
    file_dir.mkdir(parents=True)
    if site.kind == "viz":
        (root / "viz" / "server.py").write_text("")   # viz/server.py exists (cell d)
    if with_marker:
        (root / MARKER).write_text("")
    return Layout(base=base, root=root, above=root.parent, file_dir=file_dir)


def put(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def plant(site: Site, p: Path) -> Path:
    if site.name == "engram_signature":
        put(p / "server.py")     # the signature lookup needs BOTH files in one directory
        return put(p / site.plant)
    if site.name == "engram-surface-hook":
        return put(p / site.plant, (SRC / "engram_search_rules.py").read_text())
    return put(p / site.plant)


def run_site(site: Site, lay: Layout, tmp: Path, plugin_root=None):
    fixture = lay.file_dir / Path(site.real).name
    fixture.write_text("# fixture placeholder; the real source is exec'd\n")
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    empty_rt = tmp / "empty_runtime"
    empty_rt.mkdir(exist_ok=True)
    spec = dict(real=str(SRC / site.real), fixture=str(fixture), mode=site.mode,
                funcs=site.funcs + ["_bounded_parents", "bounded_parents", "_own_tree_window"], entry=site.entry,
                args=site.args, expr=site.expr, consts=site.consts,
                sys_path=[str(HOOKS)], preamble=site.preamble, src=str(SRC))
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env.update(ENGRAM_HOME=str(home), HOME=str(home))
    if site.name == "engram-surface-hook":
        env["ENGRAM_RUNTIME_DIR"] = str(empty_rt)  # forces the walk-parents fallback
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    proc = subprocess.run([sys.executable, "-I", "-c", DRIVER, json.dumps(spec)],
                          capture_output=True, text=True, env=env, timeout=60)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("RESULT:")]
    assert lines, f"driver failed for {site.name}:\n{proc.stdout}\n{proc.stderr}"
    res = json.loads(lines[-1][len("RESULT:"):])
    if res is None:
        return None
    p = Path(res)
    if site.to_file == "file":
        return p
    return p / site.to_file.split("/", 1)[1]


FLAVOURS = ["installed", "source"]
PARAMS = [pytest.param(s, f, id=f"{s.name}-{f}") for s in SITES for f in FLAVOURS]


@pytest.mark.parametrize("site,flavour", PARAMS)
def test_a_target_in_tree_nearest_copy_wins(site, flavour, tmp_path):
    lay = build_layout(tmp_path, flavour, site)
    near = plant(site, lay.file_dir.parent if site.kind != "root" else lay.root)
    plant(site, lay.root)
    plant(site, lay.above)  # a decoy further away must never win
    if site.kind == "root" or lay.file_dir.parent == lay.root:
        want = plant(site, lay.root)  # parent == root: nearest copy IS the root copy
    else:
        want = near
    got = run_site(site, lay, tmp_path)
    assert got == want, f"{site.name}: expected nearest in-tree copy {want}, got {got}"


@pytest.mark.parametrize("site,flavour", PARAMS)
def test_b_target_only_above_tree_root_is_absent(site, flavour, tmp_path):
    lay = build_layout(tmp_path, flavour, site, with_marker=not site.marker_is_target)
    plant(site, lay.above)
    if site.plant_alt:
        put(lay.above / site.plant_alt)
        if site.name == "engram-surface-hook":
            plant(site, lay.above / "src" / "engram")
    got = run_site(site, lay, tmp_path)  # CLAUDE_PLUGIN_ROOT unset
    assert got is None, f"{site.name}: matched above its tree root: {got}"


@pytest.mark.parametrize("site,flavour", PARAMS)
def test_b2_alt_shape_above_tree_root_is_absent(site, flavour, tmp_path):
    """The per-ancestor second shape (p/src/engram) must not escape either."""
    if not site.plant_alt:
        pytest.skip("site has one candidate shape")
    lay = build_layout(tmp_path, flavour, site, with_marker=not site.marker_is_target)
    alt = lay.above / site.plant_alt
    if site.name == "engram-surface-hook":
        plant(site, lay.above / "src" / "engram")
    else:
        put(alt)
    got = run_site(site, lay, tmp_path)
    assert got is None, f"{site.name}: alt shape matched above its tree: {got}"


@pytest.mark.parametrize("site,flavour", PARAMS)
def test_c_plugin_root_checked_first(site, flavour, tmp_path):
    if not site.plugin_root:
        pytest.skip("site does not consult CLAUDE_PLUGIN_ROOT")
    lay = build_layout(tmp_path, flavour, site, with_marker=not site.marker_is_target)
    pr = tmp_path / "pluginroot"
    want = plant(site, pr)  # CLAUDE_PLUGIN_ROOT/<plant> is what every site checks
    plant(site, lay.above)
    assert run_site(site, lay, tmp_path, plugin_root=pr) == want  # no in-tree copy
    if not site.sibling_first:
        if not site.marker_is_target:
            (lay.root / MARKER).write_text("")
        plant(site, lay.root)
        assert run_site(site, lay, tmp_path, plugin_root=pr) == want  # in-tree copy exists too


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("site", [s for s in SITES if s.kind == "viz"], ids=lambda s: s.name)
def test_d_viz_server_py_does_not_stop_the_walk(site, flavour, tmp_path):
    lay = build_layout(tmp_path, flavour, site)
    assert (lay.root / "viz" / "server.py").exists()
    want = plant(site, lay.root)
    assert run_site(site, lay, tmp_path) == want


@pytest.mark.parametrize("site,flavour", PARAMS)
def test_e_no_marker_searches_own_dir_and_parent_only(site, flavour, tmp_path):
    if site.marker_is_target:
        pytest.skip("the marker is the target: covered by cell (b)")
    lay = build_layout(tmp_path, flavour, site, with_marker=False)
    near = lay.file_dir if site.kind != "root" else lay.root
    parent_dir = near.parent
    want = plant(site, parent_dir)
    assert run_site(site, lay, tmp_path) == want, "parent of the file's dir must be searched"
    (parent_dir / site.plant).unlink()
    plant(site, parent_dir.parent)  # grandparent only
    assert run_site(site, lay, tmp_path) is None, "grandparent must NOT be searched"


# --------------------------------------------------------------- drift

GROUP_A_FILES = sorted({s.real for s in SITES})
HELPER_NAMES = ("_bounded_parents", "bounded_parents", "_own_tree_window")


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _lookup_calls(tree: ast.AST) -> int:
    """Calls to a bounded walk that actually feed a lookup: the iterable of a
    ``for`` loop, or the argument of ``<list>.extend(...)``.  A ``def`` line, a
    mention in a comment, or an import can never count."""
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call) \
                and _callee(node.iter) in HELPER_NAMES:
            n += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "extend":
            n += sum(isinstance(a, ast.Call) and _callee(a) in HELPER_NAMES for a in node.args)
    return n


def test_every_group_a_file_calls_the_bounded_walk():
    for rel in GROUP_A_FILES:
        text = (SRC / rel).read_text()
        tree = ast.parse(text)
        want = sum(1 for s in SITES if s.real == rel)
        got = _lookup_calls(tree)
        assert got >= want, (f"{rel}: {got} bounded-walk lookup call(s), expected {want}: "
                             "the lookup is unbounded again, or only the helper's def remains (#3983)")
        for d in ast.walk(tree):
            if isinstance(d, ast.FunctionDef) and d.name in HELPER_NAMES:
                assert MARKER in ast.get_source_segment(text, d), (
                    f"{rel}: {d.name} does not name {MARKER}")


def test_the_drift_check_rejects_a_def_only_file():
    """The check must be able to fail: a helper definition beside an unbounded loop is not a call."""
    src = ("def _bounded_parents(here):\n    return list(here.parents)\n"
           "for p in Path(__file__).resolve().parents:\n    pass\n")
    assert _lookup_calls(ast.parse(src)) == 0
    assert _lookup_calls(ast.parse("for p in _bounded_parents(x):\n    pass\n")) == 1
    assert _lookup_calls(ast.parse("r.extend(owner_read.bounded_parents(x))\n")) == 1


def _load_copy(rel: str):
    text = (SRC / rel).read_text()
    for n in ast.parse(text).body:
        if isinstance(n, ast.FunctionDef) and n.name in ("_bounded_parents", "bounded_parents"):
            g = {"Path": Path}
            exec(compile(ast.Module(body=[n], type_ignores=[]), rel, "exec"), g)
            return g[n.name]
    return None


def _copies():
    copies = {rel: _load_copy(rel) for rel in GROUP_A_FILES}
    return {k: v for k, v in copies.items()
            if v is not None and k != "tools/cites_step3.py"}


def test_every_local_copy_of_bounded_parents_behaves_identically(tmp_path):
    copies = _copies()
    assert len(copies) >= 8, sorted(copies)
    marked = tmp_path / "a" / "root"
    (marked / "x" / "y").mkdir(parents=True)
    (marked / MARKER).write_text("")
    bare = tmp_path / "b" / "p" / "q" / "r"
    bare.mkdir(parents=True)
    for rel, fn in copies.items():
        f1 = (marked / "x" / "y" / "f.py").resolve()
        got = [Path(p) for p in fn(f1)]
        assert got == [marked / "x" / "y", marked / "x", marked], rel
        f2 = (bare / "f.py").resolve()
        assert [Path(p) for p in fn(f2)] == [bare, bare.parent], rel


@pytest.mark.parametrize("raising", [PermissionError, OSError])
def test_every_copy_skips_an_unreadable_ancestor_and_keeps_walking(tmp_path, monkeypatch, raising):
    """The ``except OSError`` branch, reached on EVERY Python.  On 3.14 ``Path.exists()``
    swallows PermissionError itself, so a real chmod-000 directory never reaches the
    branch; patching ``exists`` to raise does.  A copy that STOPPED at the unreadable
    directory would return [y, x] and never reach the root."""
    copies = _copies()
    assert len(copies) >= 8, sorted(copies)
    root = (tmp_path / "root").resolve()
    (root / "x" / "y").mkdir(parents=True)
    (root / MARKER).write_text("")
    unreadable = root / "x" / MARKER   # the nearer ancestor ``x`` cannot be stat'ed
    real_exists = Path.exists

    def exists(self, *a, **kw):
        if self == unreadable:
            raise raising(13, "Permission denied", str(self))
        return real_exists(self, *a, **kw)

    monkeypatch.setattr(Path, "exists", exists)
    f = root / "x" / "y" / "f.py"
    for rel, fn in copies.items():
        got = [Path(p) for p in fn(f)]
        assert got == [root / "x" / "y", root / "x", root], f"{rel}: stopped at the unreadable dir: {got}"
