"""#3863 step 3d: tools/_common.engram_home() delegates to the shared tools-layer resolver.

Contract (written before the change):
 1. ENGRAM_HOME unset -> Path(expanduser("~/.engram")), absolute; no SystemExit, no stderr line.
 2. ENGRAM_HOME=/abs/x -> Path("/abs/x").
 3. ENGRAM_HOME="~/foo" -> the expanded absolute path.
 4. ENGRAM_HOME relative -> ValueError naming the value (the resolver's own refusal).
 5. Same value as tools._owner_read.engram_home() in every case.
Also: the flat entry telegram_bot.py uses (`sys.path.insert(tools dir); from _common import ...`)
must still import, from any cwd.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS_SRC = ROOT / "src" / "engram" / "tools"

from tools import _owner_read  # noqa: E402


def _load_common():
    spec = importlib.util.spec_from_file_location("_common_under_test", TOOLS_SRC / "_common.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def common():
    return _load_common()


def test_unset_returns_default_without_exit_or_stderr(common, monkeypatch, capsys):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    got = common.engram_home()
    assert got == Path(os.path.expanduser("~/.engram"))
    assert got.is_absolute()
    assert capsys.readouterr().err == ""
    assert got == _owner_read.engram_home()


def test_absolute_value(common, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", "/abs/x")
    assert common.engram_home() == Path("/abs/x")
    assert common.engram_home() == _owner_read.engram_home()


def test_tilde_value_expanded(common, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", "~/foo")
    got = common.engram_home()
    assert got == Path(os.path.expanduser("~/foo"))
    assert got.is_absolute()
    assert got == _owner_read.engram_home()


def test_relative_value_refused_naming_it(common, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", "rel/dir")
    with pytest.raises(ValueError, match="rel/dir"):
        common.engram_home()
    with pytest.raises(ValueError, match="rel/dir"):
        _owner_read.engram_home()


@pytest.mark.parametrize("entry", [TOOLS_SRC, ROOT / "tools"])
def test_flat_import_entry_still_works(entry, tmp_path):
    """telegram_bot.py's shape: tools dir on sys.path, `from _common import ...`, foreign cwd."""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from _common import engram_home\n"
        "print(engram_home())\n" % str(entry)
    )
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "PYTHONPATH")}
    env["ENGRAM_HOME"] = "/abs/x"
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "/abs/x"
