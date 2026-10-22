"""#3863 PR 6b: the ENGRAM-home resolver exists once per SELF-CONTAINED layer, pinned identical.

Borges's amended R4: the `resolver` allowlist class holds one entry per self-contained layer, each
AST-identical to the canonical body, `tools/_owner_read.engram_home()`, exactly as #3628 pins the
I/O helpers. Layers: tools (the canonical), hooks (`_hooklib.py`, which must stay self-contained),
prompt hooks (`_prompthooklib.py`, which must keep running when `_hooklib` cannot import), and the
shared-bin flat copies (`forum_api.py`: ia and forum run as copies in /home/agents-shared/bin with
only that module beside them, so they cannot import `_owner_read`), and (#3863 6c) the build engine
(`src/build/engine/flows.py`, which ships as tools/engine and does not import from src/engram): five layers.

  (a) TEXTUAL: the function body (docstring dropped) is AST-identical in all five files.
  (b) BEHAVIOURAL: one cell set over all five layers: env set, unset, empty (= unset), relative
      (-> refused, naming the value), tilde (-> expanded).
  (c) IDENTICAL REFUSAL (#3863 step 1): one relative value fed to every refusing entry point
      (the five resolvers, the engine's ctx and upgrade_flow, the engine CLI line, engram-snapshot's line)
      gets the same refusal text.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
LAYERS = {
    "tools": SRC / "tools" / "_owner_read.py",
    "hooks": SRC / "hooks" / "claude" / "_hooklib.py",
    "prompt-hooks": SRC / "hooks" / "claude" / "_prompthooklib.py",
    "shared-bin": SRC / "tools" / "forum_api.py",
    # #3863 6c: the build engine is its own self-contained layer (src/build/engine ships as tools/engine and
    # does not import from src/engram), so it carries one pinned copy, in flows.py (cli.py imports it from there).
    "build-engine": REPO / "src" / "build" / "engine" / "flows.py",
    # #3863 step 2: the runtime layer (the flat modules engram_core, the owner/client, the server and the
    # hook-imported emitter/store/indexer import) is one shared module, not a copy per module.
    # Ruling: Borges's step-2 rulings on #3863, 2026-10-21.
    "runtime": SRC / "engram_home_resolver.py",
}


def _func(path: Path) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "engram_home"]
    assert len(hits) == 1, f"{path.name}: expected exactly one top-level engram_home, found {len(hits)}"
    return hits[0]


def _dump_without_docstring(fn: ast.FunctionDef) -> str:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    clone = ast.FunctionDef(name=fn.name, args=fn.args, body=body, decorator_list=fn.decorator_list,
                            returns=fn.returns, type_comment=None, type_params=[])
    return ast.dump(ast.fix_missing_locations(clone))


def test_p1_the_three_layers_define_the_resolver_exactly_once_each():
    for layer, path in LAYERS.items():
        _func(path)


def test_p2_bodies_are_ast_identical_to_the_canonical():
    canon = _dump_without_docstring(_func(LAYERS["tools"]))
    for layer, path in LAYERS.items():
        assert _dump_without_docstring(_func(path)) == canon, f"{layer} drifted from tools/_owner_read.engram_home()"


def _load(layer: str):
    path = LAYERS[layer]
    if layer == "build-engine":
        # flows.py uses package-relative imports, so it cannot be exec'd by path; import it as the package
        # tests/conftest.py already aliases (tools.engine), the way the engine's own tests do.
        return importlib.import_module("tools.engine.flows")
    name = f"_p3863_{layer.replace('-', '_')}"  # the runtime layer is a flat module with no sibling imports
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        if layer == "tools":
            sys.path.insert(0, str(SRC))
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
        if layer == "tools" and sys.path and sys.path[0] == str(SRC):
            sys.path.pop(0)
    return mod


@pytest.mark.parametrize("layer", list(LAYERS))
def test_b1_env_set_absolute(layer, monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "my engram's home"))
    assert _load(layer).engram_home() == tmp_path / "my engram's home"


@pytest.mark.parametrize("layer", list(LAYERS))
@pytest.mark.parametrize("how", ["unset", "empty"])
def test_b2_unset_and_empty_mean_the_default(layer, how, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    if how == "unset":
        monkeypatch.delenv("ENGRAM_HOME", raising=False)
    else:
        monkeypatch.setenv("ENGRAM_HOME", "")
    assert _load(layer).engram_home() == tmp_path / ".engram"


@pytest.mark.parametrize("layer", list(LAYERS))
@pytest.mark.parametrize("value", ["rel/dir", ".", "..", "./x", "~nosuchuser_3863/x"])
def test_b3_a_relative_value_is_refused_and_named(layer, value, monkeypatch, tmp_path):
    """Borges's ruling (#3863 6b): a relative ENGRAM_HOME resolves against the CALLING process's cwd, so a
    hook, the owner daemon and the MCP server would each land in a different home. It is unsupported:
    the one resolver body refuses it with an error naming the value. Mutation: restore abspath-of-relative
    -> returns a path -> red."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    mod = _load(layer)  # load first: _hooklib resolves the home at import (and exits 0 on a refused one)
    monkeypatch.setenv("ENGRAM_HOME", value)
    with pytest.raises(ValueError) as e:
        mod.engram_home()
    assert value in str(e.value)


@pytest.mark.parametrize("layer", list(LAYERS))
def test_b5_an_absolute_value_with_dotdot_is_normalized(layer, monkeypatch, tmp_path):
    """abspath is behaviourally pinned (round-2 fairy: the AST test alone caught a dropped abspath).
    Mutation: drop abspath -> the path keeps its `..` -> red."""
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    mod = _load(layer)
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "a" / ".." / "b"))
    assert mod.engram_home() == tmp_path / "b"


@pytest.mark.parametrize("layer", list(LAYERS))
def test_b4_tilde_is_expanded(layer, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAM_HOME", "~/elsewhere")
    assert _load(layer).engram_home() == tmp_path / "elsewhere"


# ---------------------------------------------------------------------------
# The identical-refusal cell (#3863 step 1, round 2). Ruling:
# Borges's ruling on #3863, 2026-10-21.
#
# ONE relative value goes to EVERY refusing entry point and each must refuse with the SAME text, naming
# the value. Only the surrounding stderr prefix (a per-entry-point constant below) is normalised away; the
# prefix is asserted too, so a path that drops or invents a prefix is also red. Each entry point is its own
# parametrised cell, so a path that accepts the relative value reddens ONLY its own cell.
# ---------------------------------------------------------------------------

REFUSED_VALUE = "rel/dir"
REFUSAL_TEXT = "ENGRAM_HOME must be an absolute path, got 'rel/dir'"  # the one text, pinned verbatim
_ENGINE = "[engine] ERROR: "


def _refusal_resolver(layer):
    def run(monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ENGRAM_HOME", raising=False)
        mod = _load(layer)  # load first: _hooklib resolves the home at import (and exits 0 on a refused one)
        monkeypatch.setenv("ENGRAM_HOME", REFUSED_VALUE)
        with pytest.raises(ValueError) as e:
            mod.engram_home()
        return "", str(e.value)
    return run


def _refusal_runtime_or_none(monkeypatch, tmp_path, capsys):
    """The hook-imported modules' variant: engram_home_or_none() never raises, prints ONE stderr line
    carrying the same refusal text, and returns None."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    mod = _load("runtime")
    monkeypatch.setenv("ENGRAM_HOME", REFUSED_VALUE)
    capsys.readouterr()
    assert mod.engram_home_or_none() is None
    assert mod.engram_home_or_none() is None
    lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
    assert len(lines) == 1, lines
    return "engram_home_resolver: ", lines[0].removesuffix("; writes disabled in this process")


def _refusal_cli_ctx(monkeypatch, tmp_path, capsys):
    from tools.engine import cli
    monkeypatch.setenv("ENGRAM_HOME", REFUSED_VALUE)
    with pytest.raises(ValueError) as e:
        cli._build_ctx_from_env()
    return "", str(e.value)


def _refusal_upgrade_flow(monkeypatch, tmp_path, capsys):
    from tools.engine import flows
    monkeypatch.setenv("ENGRAM_HOME", REFUSED_VALUE)
    with pytest.raises(ValueError) as e:
        flows.upgrade_flow()
    return "", str(e.value)


def _refusal_cli_main(monkeypatch, tmp_path, capsys):
    from tools.engine import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REFUSED_VALUE)
    assert cli.main(["plan", "upgrade"]) == 1
    err = capsys.readouterr().err
    lines = [l for l in err.splitlines() if l.strip()]
    assert len(lines) == 1, err
    return _ENGINE, lines[0]


def _refusal_snapshot(monkeypatch, tmp_path, capsys):
    import subprocess
    env = {**os.environ, "ENGRAM_HOME": REFUSED_VALUE}
    r = subprocess.run([sys.executable, str(SRC / "tools" / "engram-snapshot"), "--help"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, (r.returncode, r.stderr)
    lines = [l for l in r.stderr.splitlines() if l.strip()]
    assert len(lines) == 1, r.stderr
    return "engram-snapshot: ", lines[0]


REFUSING_ENTRY_POINTS = {
    "tools._owner_read.engram_home": _refusal_resolver("tools"),
    "hooks._hooklib.engram_home": _refusal_resolver("hooks"),
    "prompt-hooks._prompthooklib.engram_home": _refusal_resolver("prompt-hooks"),
    "shared-bin.forum_api.engram_home": _refusal_resolver("shared-bin"),
    "build-engine.flows.engram_home": _refusal_resolver("build-engine"),
    "runtime.engram_home_resolver.engram_home": _refusal_resolver("runtime"),
    "runtime.engram_home_resolver.engram_home_or_none (stderr line)": _refusal_runtime_or_none,
    "build-engine.cli._build_ctx_from_env": _refusal_cli_ctx,
    "build-engine.flows.upgrade_flow": _refusal_upgrade_flow,
    "build-engine.cli.main (stderr line)": _refusal_cli_main,
    "engram-snapshot (stderr line)": _refusal_snapshot,
}


@pytest.mark.parametrize("entry", list(REFUSING_ENTRY_POINTS))
def test_r1_every_refusing_entry_point_gives_the_identical_refusal(entry, monkeypatch, tmp_path, capsys):
    """Mutation (run in the round-2 report): make ONE path accept the relative value -> only that entry's
    cell is red, every other cell stays green."""
    prefix, text = REFUSING_ENTRY_POINTS[entry](monkeypatch, tmp_path, capsys)
    assert text.startswith(prefix), (entry, text)
    assert text[len(prefix):] == REFUSAL_TEXT, (entry, text)
    assert REFUSED_VALUE in text[len(prefix):]


def test_r2_the_cell_covers_every_layer_in_the_parity_table():
    covered = {name.split(".")[0] for name in REFUSING_ENTRY_POINTS}
    assert set(LAYERS) <= covered, set(LAYERS) - covered


# ---------------------------------------------------------------------------
# #3863 step 3a: the four inline-guard hooks each carry a VERBATIM pinned copy of the canonical body.
# Ruling: Borges's rulings on #3863, 2026-10-21 (a standalone installed script carries a verbatim pinned copy).
# These hooks cannot be exec'd for the behavioural cells (they run work at import), so the function is lifted
# out of the file by AST and exec'd in a bare namespace.
# ---------------------------------------------------------------------------

STANDALONE_HOOKS = {
    "tripwire": SRC / "hooks" / "claude" / "engram-lesson-tripwire-hook.py",
    "loop-arm-stop": SRC / "hooks" / "claude" / "engram-loop-arm-stop-hook.py",
    "stale-tree-warn": SRC / "hooks" / "claude" / "engram-stale-tree-warn-hook.py",
    "time-bar": SRC / "hooks" / "claude" / "engram-time-bar-hook.py",
}


def _lifted(path: Path):
    fn = _func(path)
    ns = {"os": os, "Path": Path}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(path), "exec"), ns)
    return ns["engram_home"]


@pytest.mark.parametrize("hook", list(STANDALONE_HOOKS))
def test_s1_a_standalone_hook_copy_is_ast_identical_to_the_canonical(hook):
    canon = _dump_without_docstring(_func(LAYERS["tools"]))
    assert _dump_without_docstring(_func(STANDALONE_HOOKS[hook])) == canon, f"{hook} drifted"


@pytest.mark.parametrize("hook", list(STANDALONE_HOOKS))
def test_s2_a_standalone_hook_copy_behaves_like_the_canonical(hook, monkeypatch, tmp_path):
    fn = _lifted(STANDALONE_HOOKS[hook])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    assert fn() == tmp_path / ".engram"
    monkeypatch.setenv("ENGRAM_HOME", "")
    assert fn() == tmp_path / ".engram"
    monkeypatch.setenv("ENGRAM_HOME", "~/e")
    assert fn() == tmp_path / "e"
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "a" / ".." / "b"))
    assert fn() == tmp_path / "b"
    monkeypatch.setenv("ENGRAM_HOME", "rel/dir")
    with pytest.raises(ValueError) as e:
        fn()
    assert str(e.value) == REFUSAL_TEXT


def test_s3_the_standalone_hooks_are_pinned_in_the_gate_allowlist():
    spec = importlib.util.spec_from_file_location("_gate_for_step3a", REPO / "tools" / "raw_home_read_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    for path in STANDALONE_HOOKS.values():
        assert str(path.relative_to(REPO)) in gate.PINNED_RESOLVERS, path
