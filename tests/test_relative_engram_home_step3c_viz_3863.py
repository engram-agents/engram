"""#3863 step 3c-i: the three src/engram/viz sites stop reading ENGRAM_HOME raw.

Ruling: Borges's rulings on #3863 (comment 6384125841, 2026-10-24); the rulings themselves are cited in
engram_home_resolver's docstring. Viz is a PROCESS ENTRY POINT, so:
  site 1  viz/agents.py `_ENGRAM_HOME` / `DEFAULT_DB` (import-time constant): `engram_home()`; a RELATIVE value
          raises ValueError at import of viz.agents (and so of the viz server entry), no wrapper.
  site 2  viz/agents.py `_own_agent_name()` (documented never-raise): `engram_home_or_none()`, None -> ''.
  site 3  viz/version_info.py `read_deployed_version()` (documented never-raise): `engram_home_or_none()`,
          None -> {}.
Asymmetry (stated, tested): with a relative value `import viz.version_info` succeeds, `import viz.agents` raises.

Every cell runs in a SUBPROCESS (site 1 is an import-time constant; no importlib.reload, no in-process state).
Three layouts are measured, because the resolver is a FLAT module and viz is imported both ways:
  pp_src   PYTHONPATH=<repo>/src            module `engram.viz.*`   (python -m engram.viz.server from a checkout)
  srceng   PYTHONPATH=<repo>/src/engram     module `viz.*`          (how the test suite imports viz)
  flat     a tmp copy of src/engram/ with no engram/ wrapper, cwd = that root, module `viz.*` (deployed plugin)
The import form is the sqlite_ro pair: try the flat name, fall back to the package-qualified one. A ValueError is
not an ImportError, so a relative value must still raise through the fallback (cells `*-rel`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SRC_ENGRAM = SRC / "engram"
REL = "rel/x"
LAYOUTS = ["pp_src", "srceng", "flat"]
PKG = {"pp_src": "engram.viz", "srceng": "viz", "flat": "viz"}


@pytest.fixture(scope="module")
def flat_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("flat") / "plugin"
    shutil.copytree(SRC_ENGRAM, root, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    # the flat cell's cwd IS the plugin root, so the decoy a wrongly-accepted relative value would act on lives here
    (root / "rel" / "x").mkdir(parents=True)
    (root / "rel" / "x" / "config.json").write_text(json.dumps({"agent_name": "decoy"}))
    (root / "rel" / "x" / ".deployed-version").write_text("alpha_tag=decoy\n")
    return root


def _layout(name, flat_root):
    """-> (extra env, module prefix, cwd override or None)"""
    if name == "pp_src":
        return {"PYTHONPATH": str(SRC)}, PKG[name], None
    if name == "srceng":
        return {"PYTHONPATH": str(SRC_ENGRAM)}, PKG[name], None
    return {}, PKG[name], flat_root


def _env(home, extra=None, **env_home):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}
    env.update({"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra or {})
    env.update(env_home)
    return env


def _snapshot(*roots):
    out = {}
    for root in roots:
        for p, dirs, files in os.walk(root):
            for n in dirs:
                out[str(Path(p) / n)] = ("dir", 0, "")
            for n in files:
                fp = Path(p) / n
                out[str(fp)] = ("file", fp.stat().st_size, hashlib.sha256(fp.read_bytes()).hexdigest())
    return out


def _snap(layout, flat_root, home, cwd):
    return _snapshot(home, cwd, *([flat_root] if layout == "flat" else []))


def _run(code, layout, flat_root, home, cwd, **env_home):
    extra, prefix, cwd_over = _layout(layout, flat_root)
    code = code.replace("PKG", prefix)
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd_over or cwd),
                          env=_env(home, extra, **env_home), capture_output=True, text=True, timeout=120)


@pytest.fixture
def world(tmp_path):
    """home = HOME of the subprocess; cwd = a scratch cwd holding a decoy fixture under rel/x."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    (cwd / "rel" / "x").mkdir(parents=True)
    (cwd / "rel" / "x" / "config.json").write_text(json.dumps({"agent_name": "decoy"}))
    (cwd / "rel" / "x" / ".deployed-version").write_text("alpha_tag=decoy\n")
    home.mkdir()
    return home, cwd


def _plant_home(root, name="alice"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"agent_name": name}))
    (root / ".deployed-version").write_text("alpha_tag=v1\nsha = abc\n")


# ------------------------------------------------------------------------------------------------ site 1

DB_CODE = "import PKG.agents as a; print(a.DEFAULT_DB)"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_site1_rel_raises_at_import_naming_the_value_and_creates_nothing(layout, flat_root, world):
    home, cwd = world
    before = _snap(layout, flat_root, home, cwd)
    r = _run(DB_CODE, layout, flat_root, home, cwd, ENGRAM_HOME=REL)
    assert r.returncode != 0, r.stdout
    # ValueError reaches the top level through the `except ImportError` fallback (it is not an ImportError).
    assert "ValueError" in r.stderr and "'rel/x'" in r.stderr, r.stderr
    assert r.stdout == ""
    assert _snap(layout, flat_root, home, cwd) == before


def test_site1_server_entry_refuses_a_relative_value(world):
    home, cwd = world
    before = _snapshot(home, cwd)
    r = subprocess.run([sys.executable, "-m", "engram.viz.server", "--help"], cwd=str(cwd),
                       env=_env(home, {"PYTHONPATH": str(SRC)}, ENGRAM_HOME=REL),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    assert "ValueError" in r.stderr and "'rel/x'" in r.stderr, r.stderr
    assert _snapshot(home, cwd) == before


@pytest.mark.parametrize("layout", LAYOUTS)
def test_site1_unset(layout, flat_root, world):
    home, cwd = world
    r = _run(DB_CODE, layout, flat_root, home, cwd)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(home / ".engram" / "knowledge.db")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_site1_abs(layout, flat_root, world, tmp_path):
    home, cwd = world
    r = _run(DB_CODE, layout, flat_root, home, cwd, ENGRAM_HOME=str(tmp_path / "h"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / "h" / "knowledge.db")


def test_site1_empty_is_default_home(world):
    home, cwd = world
    r = _run(DB_CODE, "srceng", None, home, cwd, ENGRAM_HOME="")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(home / ".engram" / "knowledge.db")


def test_site1_tilde_expands(world):
    home, cwd = world
    r = _run(DB_CODE, "srceng", None, home, cwd, ENGRAM_HOME="~/x")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(home / "x" / "knowledge.db")


# ------------------------------------------------------------------------------------------------ site 2
# Site 1 raises at import with a relative value, so site 2 is reached by importing with a VALID home first and
# changing os.environ inside the subprocess before calling (the only route to `_own_agent_name` with that value).

OWN_CODE = (
    "import os, sys; import PKG.agents as a\n"
    "os.environ[{key!r}] = {val!r}\n" "{unset}"
    "print(repr(a._own_agent_name()))"
)


def _own(layout, flat_root, home, cwd, val, valid_home):
    unset = "" if val is not None else "del os.environ['ENGRAM_HOME']\n"
    code = OWN_CODE.format(key="ENGRAM_HOME", val=val or "", unset=unset)
    return _run(code, layout, flat_root, home, cwd, ENGRAM_HOME=str(valid_home))


@pytest.mark.parametrize("layout", LAYOUTS)
def test_site2_rel_returns_empty_string_and_does_not_raise(layout, flat_root, world, tmp_path):
    home, cwd = world
    valid = tmp_path / "valid"
    _plant_home(valid, "valid-agent")
    # a decoy at the DEFAULT home: a wrong refusal that falls back to ~/.engram would return 'dflt', not ''
    _plant_home(home / ".engram", "dflt")
    before = _snap(layout, flat_root, home, cwd)
    r = _own(layout, flat_root, home, cwd, REL, valid)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "''"          # NOT 'decoy' (cwd/rel/x/config.json) and not 'valid-agent'
    # engram_home_or_none() prints its ONE refusal line (naming the value); a mutant using engram_home() inside
    # the never-raise try/except would also return '' / {} but print nothing, so the line is asserted.
    assert [l for l in r.stderr.splitlines() if l.strip()] == \
        ["engram_home_resolver: ENGRAM_HOME must be an absolute path, got 'rel/x'; writes disabled in this process"], r.stderr
    assert _snap(layout, flat_root, home, cwd) == before


@pytest.mark.parametrize("layout", LAYOUTS)
def test_site2_abs_reads_agent_name(layout, flat_root, world, tmp_path):
    home, cwd = world
    h = tmp_path / "h"
    _plant_home(h, "alice")
    r = _own(layout, flat_root, home, cwd, str(h), tmp_path / "other")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "'alice'"


def test_site2_unset_empty_tilde(world, tmp_path):
    home, cwd = world
    _plant_home(home / ".engram", "dflt")
    _plant_home(home / "x", "tilde")
    other = tmp_path / "other"
    assert _own("srceng", None, home, cwd, None, other).stdout.strip() == "'dflt'"
    assert _own("srceng", None, home, cwd, "", other).stdout.strip() == "'dflt'"
    assert _own("srceng", None, home, cwd, "~/x", other).stdout.strip() == "'tilde'"


# ------------------------------------------------------------------------------------------------ site 3

VER_CODE = ("import os, json; import PKG.version_info as v\n"
            "arg = os.environ.get('VI_ARG')\n"
            "print(json.dumps(v.read_deployed_version(arg) if arg else v.read_deployed_version(), sort_keys=True))")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_site3_rel_returns_empty_dict_no_raise_no_write(layout, flat_root, world):
    home, cwd = world
    # a decoy at the DEFAULT home: a wrong refusal that falls back to ~/.engram would return its dict, not {}
    _plant_home(home / ".engram")
    before = _snap(layout, flat_root, home, cwd)
    r = _run(VER_CODE, layout, flat_root, home, cwd, ENGRAM_HOME=REL)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "{}"          # NOT {"alpha_tag": "decoy"} from cwd/rel/x
    # engram_home_or_none() prints its ONE refusal line (naming the value); a mutant using engram_home() inside
    # the never-raise try/except would also return '' / {} but print nothing, so the line is asserted.
    assert [l for l in r.stderr.splitlines() if l.strip()] == \
        ["engram_home_resolver: ENGRAM_HOME must be an absolute path, got 'rel/x'; writes disabled in this process"], r.stderr
    assert _snap(layout, flat_root, home, cwd) == before


@pytest.mark.parametrize("layout", LAYOUTS)
def test_site3_abs_reads_file(layout, flat_root, world, tmp_path):
    home, cwd = world
    _plant_home(tmp_path / "h")
    r = _run(VER_CODE, layout, flat_root, home, cwd, ENGRAM_HOME=str(tmp_path / "h"))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"alpha_tag": "v1", "sha": "abc"}


def test_site3_unset_empty_tilde(world):
    home, cwd = world
    _plant_home(home / ".engram")
    _plant_home(home / "x")
    (home / "x" / ".deployed-version").write_text("alpha_tag=tilde\n")
    assert json.loads(_run(VER_CODE, "srceng", None, home, cwd).stdout) == {"alpha_tag": "v1", "sha": "abc"}
    assert json.loads(_run(VER_CODE, "srceng", None, home, cwd, ENGRAM_HOME="").stdout) == \
        {"alpha_tag": "v1", "sha": "abc"}
    assert json.loads(_run(VER_CODE, "srceng", None, home, cwd, ENGRAM_HOME="~/x").stdout) == {"alpha_tag": "tilde"}


def test_site3_explicit_parameter_is_unchanged_even_with_relative_env(world, tmp_path):
    home, cwd = world
    _plant_home(tmp_path / "p")
    r = _run(VER_CODE, "srceng", None, home, cwd, ENGRAM_HOME=REL, VI_ARG=str(tmp_path / "p"))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"alpha_tag": "v1", "sha": "abc"}
    assert r.stderr == ""                     # the explicit path never consults the resolver


# ------------------------------------------------------------------------------------------------ the asymmetry

@pytest.mark.parametrize("layout", LAYOUTS)
def test_asymmetry_version_info_is_import_safe_agents_is_not(layout, flat_root, world):
    home, cwd = world
    ok = _run("import PKG.version_info", layout, flat_root, home, cwd, ENGRAM_HOME=REL)
    bad = _run("import PKG.agents", layout, flat_root, home, cwd, ENGRAM_HOME=REL)
    assert ok.returncode == 0, ok.stderr
    assert bad.returncode != 0 and "ValueError" in bad.stderr


# ------------------------------------------------------------------------------------------------ viz.workbench

@pytest.mark.parametrize("layout", LAYOUTS)
def test_workbench_entry_refuses_a_relative_value_and_creates_nothing(layout, flat_root, world):
    """Sol's cell from the #4128 review. `python -m viz.workbench` is the entry point that viz/agents.py ALONE
    guards (`python -m engram.viz.server` already dies earlier, in engram_core). Before #4128 it exited 0 on
    ENGRAM_HOME=rel/x; it must now exit non-zero with the ValueError naming the value, in all three layouts, and
    leave nothing under the cwd or the home. `--help` makes the old (raw-read) behaviour a clean exit 0 instead of
    a server that never returns, so a regression is a red cell and not a hang."""
    home, cwd = world
    before = _snap(layout, flat_root, home, cwd)
    extra, prefix, cwd_over = _layout(layout, flat_root)
    r = subprocess.run([sys.executable, "-m", f"{prefix}.workbench", "--help"], cwd=str(cwd_over or cwd),
                       env=_env(home, extra, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert "ValueError" in r.stderr and "'rel/x'" in r.stderr, r.stderr
    assert _snap(layout, flat_root, home, cwd) == before
