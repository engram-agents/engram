"""The hook's route-ledger directory and the probe mod's default probeDir must be one directory.

alpha #3863 (6b). `_hooklib.route_probe_dir()` writes `<probeDir>/<session>/routes/...` only when that
directory already exists; the mod (tools/probes/mods-recall) creates it. If the two name different
directories the hook records nothing for a live probe, with no error. Both now follow ENGRAM_HOME.

The mod is TypeScript and cannot be imported here, so this pins it the way the AST parity tests pin the
resolver copies: read the mod's own default string and subpath constant out of its source, apply the
mod's rule (record.ts defaultProbeDir) in Python, and compare with what the hook computes.
Mutations: change either side's subpath, or make the hook ignore ENGRAM_HOME -> red.
"""
import importlib
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "tools" / "probes" / "mods-recall" / "hooks"
HOOKS = ROOT / "src" / "engram" / "hooks" / "claude"


def _mod_default() -> str:
    m = re.search(r"probeDir:\s*'([^']+)',\n}", (MOD / "config.ts").read_text())
    assert m, "config.ts DEFAULTS.probeDir not found"
    return m.group(1)


def _mod_subpath() -> str:
    m = re.search(r"PROBE_SUBPATH\s*=\s*'([^']+)'", (MOD / "record.ts").read_text())
    assert m, "record.ts PROBE_SUBPATH not found"
    return m.group(1)


def _mod_rule(engram_home, home: str):
    """record.ts relativeEngramHome + defaultProbeDir + resolveProbeDir, restated: a RELATIVE ENGRAM_HOME is
    refused (None); an absolute or ~-led one is followed, else the default string; then a leading ~ is
    expanded against HOME."""
    eh = (engram_home or "").rstrip("/")
    if eh and not (eh.startswith("/") or eh == "~" or eh.startswith("~/")):
        return None
    if eh.startswith("/") or eh == "~" or eh.startswith("~/"):
        d = eh + _mod_subpath()
    else:
        d = _mod_default()
    if d == "~" or d.startswith("~/"):
        d = home.rstrip("/") + d[1:]
    return d


@pytest.fixture()
def hooklib(monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    sys.modules.pop("_hooklib", None)
    yield importlib.import_module("_hooklib")
    sys.modules.pop("_hooklib", None)


def test_the_mod_default_and_its_subpath_name_the_same_directory():
    assert _mod_default() == "~/.engram" + _mod_subpath()


@pytest.mark.parametrize("case", ["unset", "absolute", "empty", "tilde"])
def test_hook_dir_equals_mod_default_dir(case, hooklib, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    value = {"unset": None, "absolute": str(tmp_path / "store"), "empty": "", "tilde": "~/elsewhere"}[case]
    if value is None:
        monkeypatch.delenv("ENGRAM_HOME", raising=False)
    else:
        monkeypatch.setenv("ENGRAM_HOME", value)
    assert str(hooklib.route_probe_dir()) == _mod_rule(value, str(home))


def test_a_relative_engram_home_is_refused_on_both_sides(hooklib, monkeypatch, tmp_path):
    """Borges's ruling (#3863 6b): a relative ENGRAM_HOME is unsupported. The hook's resolver refuses it
    (ValueError naming the value, so the route ledger fails open); the mod refuses it too (record.ts
    relativeEngramHome -> the probe stays off; the TS cell is in step.test.ts)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ENGRAM_HOME", "rel/store")
    with pytest.raises(ValueError, match="rel/store"):
        hooklib.route_probe_dir()
    assert _mod_rule("rel/store", str(home)) is None
    src = (MOD / "register.ts").read_text()
    assert "relativeEngramHome" in src and "relative-engram-home" in src
