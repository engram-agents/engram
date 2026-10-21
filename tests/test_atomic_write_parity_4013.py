"""#4013: the shared atomic text writer exists in TWO files on purpose, pinned identical.

`hooks/claude/_hooklib.py` must stay self-contained (every hook imports it; tests copy it alone into
synthetic hooks/ layouts), so it cannot import the core `engram_config_io.py`. Same arrangement and
same reason as tests/test_regular_io_parity_3628.py: the function body (docstring dropped, wording
may differ, code may not) is AST-identical in both. The mode/group/symlink/new-file behaviour of
every writer is the contract in tests/test_atomic_writer_modes_4013.py (W1-W5); the cells at the
bottom here close gaps a mutation pass found in it:
  - W1 uses 0o660, so "always chmod 0o660" passed it            -> mode 0o640 here.
  - W4's failure fires before the temp exists, so "never remove the temp" passed it
                                                                -> fail os.replace here.
  - no cell saw the group restore (the test dir's group is the process's)
                                                                -> record the fchown call here.
"""

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "engram"
COPIES = {
    "core": SRC / "engram_config_io.py",
    "hooks": SRC / "hooks" / "claude" / "_hooklib.py",
}


def _body_dump(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "atomic_write_text"]
    assert len(hits) == 1, f"{path.name}: expected exactly one top-level atomic_write_text"
    fn = hits[0]
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    clone = ast.FunctionDef(name=fn.name, args=fn.args, body=body, decorator_list=fn.decorator_list,
                            returns=fn.returns, type_comment=None, type_params=[])
    return ast.dump(ast.fix_missing_locations(clone))


def test_atomic_write_text_copies_are_ast_identical():
    assert _body_dump(COPIES["hooks"]) == _body_dump(COPIES["core"]), (
        "_hooklib.atomic_write_text drifted from engram_config_io.atomic_write_text"
    )


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def writer_mods():
    sys.path.insert(0, str(SRC))
    try:
        import engram_config_io
    finally:
        sys.path.remove(str(SRC))
    return {"core": engram_config_io, "hooks": _load("_hooklib_4013b", COPIES["hooks"])}


@pytest.fixture(params=["core", "hooks"])
def mod(request, writer_mods):
    return writer_mods[request.param]


def test_existing_mode_0640_is_kept_not_forced(mod, tmp_path):
    p = tmp_path / "f.json"
    p.write_text("{}")
    os.chmod(p, 0o640)
    mod.atomic_write_text(p, "{}\n")
    assert os.stat(p).st_mode & 0o777 == 0o640


def test_failed_replace_removes_the_temp_and_keeps_the_target(mod, tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    p.write_text("old")

    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError):
        mod.atomic_write_text(p, "new")
    assert p.read_text() == "old"
    assert os.listdir(tmp_path) == ["f.json"]


def test_existing_group_is_restored_before_the_mode(mod, tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    p.write_text("old")
    calls = []
    real_fchown, real_fchmod = os.fchown, os.fchmod

    def spy_chown(fd, u, g):
        calls.append(("chown", u, g))
        return real_fchown(fd, u, g)

    def spy_chmod(fd, m):
        calls.append(("chmod", m))
        return real_fchmod(fd, m)

    monkeypatch.setattr(mod.os, "fchown", spy_chown)
    monkeypatch.setattr(mod.os, "fchmod", spy_chmod)
    before = os.stat(p)
    mod.atomic_write_text(p, "new")
    assert calls == [("chown", -1, before.st_gid), ("chmod", before.st_mode & 0o7777)]


def test_fchown_permission_error_is_swallowed_and_mode_kept(mod, tmp_path, monkeypatch):
    """Pins `except PermissionError: pass`: a group we cannot join must not fail the write."""
    p = tmp_path / "f.json"
    p.write_text("old")
    os.chmod(p, 0o640)

    def denied(*a, **k):
        raise PermissionError("not a member of that group")

    monkeypatch.setattr(mod.os, "fchown", denied)
    mod.atomic_write_text(p, "new")
    assert p.read_text() == "new"
    assert os.stat(p).st_mode & 0o777 == 0o640
    assert os.listdir(tmp_path) == ["f.json"]


def test_keyboard_interrupt_in_fsync_propagates_and_cleans_up(mod, tmp_path, monkeypatch):
    """Pins `except BaseException:`: an interrupt mid-write must not leave a temp file."""
    p = tmp_path / "f.json"
    p.write_text("old")
    before = p.read_bytes()

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(mod.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        mod.atomic_write_text(p, "new")
    assert p.read_bytes() == before
    assert os.listdir(tmp_path) == ["f.json"]
