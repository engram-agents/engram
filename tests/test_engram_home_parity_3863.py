"""#3863 PR 6b: the ENGRAM-home resolver exists once per SELF-CONTAINED layer, pinned identical.

Borges's amended R4: the `resolver` allowlist class holds one entry per self-contained layer, each
AST-identical to the canonical body, `tools/_owner_read.engram_home()`, exactly as #3628 pins the
I/O helpers. Layers: tools (the canonical), hooks (`_hooklib.py`, which must stay self-contained),
prompt hooks (`_prompthooklib.py`, which must keep running when `_hooklib` cannot import), and the
shared-bin flat copies (`forum_api.py`: ia and forum run as copies in /home/agents-shared/bin with
only that module beside them, so they cannot import `_owner_read`).

  (a) TEXTUAL: the function body (docstring dropped) is AST-identical in all three files.
  (b) BEHAVIOURAL: one cell set over all three: env set, unset, empty (= unset), relative
      (-> refused, naming the value), tilde (-> expanded).
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
    name = f"_p3863_{layer.replace('-', '_')}"
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
