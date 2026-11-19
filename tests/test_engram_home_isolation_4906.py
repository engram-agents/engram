"""#4906 / #4881 -- a test process must never write into a seat's LIVE ENGRAM home.

tests/conftest.py used to give the process a throwaway ENGRAM_HOME only when
ENGRAM_HOME was ABSENT from the environment. Every seat's shell exports it, so
on the machines the suite actually runs on the guard was a no-op and a test
that forgot its own isolation wrote into the live graph. These cells pin the
unconditional form, observed from outside (a child pytest whose ambient
ENGRAM_HOME is an existing empty directory X must leave X untouched), and the
one file (#4881) that isolated with a variable nothing reads.

The module under test is loaded BY FILE PATH and registered in sys.modules
before exec (never via sys.path.insert, ls_0095). Every child gets an explicit
env (HOME and TMPDIR are scratch dirs; nothing is inherited) and connects to
nothing.
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST_PATH = REPO_ROOT / "tests" / "conftest.py"
COUNTERFACTUAL_PATH = REPO_ROOT / "tests" / "test_counterfactual_inference_quote_type.py"
THROWAWAY_PREFIX = "engram-alpha-test-fallback-home-"

_UNSET = object()  # ENGRAM_HOME absent from the child's environment

_PROBE_CONFTEST = '''
import importlib.util, sys
_spec = importlib.util.spec_from_file_location("engram_conftest_under_test", {path!r})
_mod = importlib.util.module_from_spec(_spec)
sys.modules["engram_conftest_under_test"] = _mod
_spec.loader.exec_module(_mod)
'''

_PROBE_TEST = '''
import json, os, stat, sys, tempfile
from pathlib import Path


def test_probe():
    import engram_core
    import server

    payload = {
        "quoted_text": "probe claim for the home-isolation cell.",
        "interpretation": "probe",
        "claim": "probe claim for the home-isolation cell.",
        "quote_type": "counterfactual_inference",
        "url": "https://example.com/probe",
        "title": "probe",
    }
    result = server.engram_add_observation(json.dumps(payload))
    mod = sys.modules["engram_conftest_under_test"]
    with open(os.environ["PROBE_OUT"], "w") as fh:
        json.dump(
            {
                "engram_home": os.environ.get("ENGRAM_HOME"),
                "ambient": getattr(mod, "_AMBIENT_ENGRAM_HOME", "<missing>"),
                "data_dir": str(engram_core.DATA_DIR),
                "default_home": str(Path.home() / ".engram"),
                "tempdir": tempfile.gettempdir(),
                "home_mode": stat.S_IMODE(os.stat(os.environ["ENGRAM_HOME"]).st_mode),
                "result": result,
            },
            fh,
        )
'''


def _run_child(tmp_path, ambient):
    """Run the probe in a child pytest whose ambient ENGRAM_HOME is `ambient`.

    ambient is a path string, "" (exported empty), or _UNSET (absent). Returns
    (probe_output_dict, completed_process)."""
    root = tmp_path / "child_root"
    root.mkdir()
    child_home = tmp_path / "child_home"
    child_home.mkdir()
    child_tmp = tmp_path / "child_tmp"
    child_tmp.mkdir()
    (root / "conftest.py").write_text(_PROBE_CONFTEST.format(path=str(CONFTEST_PATH)))
    (root / "test_probe.py").write_text(_PROBE_TEST)
    (root / "pytest.ini").write_text("[pytest]\n")
    out = tmp_path / "probe_out.json"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(child_home),
        "TMPDIR": str(child_tmp),
        "PROBE_OUT": str(out),
        "ENGRAM_NO_EMBEDDINGS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if ambient is not _UNSET:
        env["ENGRAM_HOME"] = ambient
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
         str(root / "test_probe.py")],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=300,
    )
    detail = f"child exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert proc.returncode == 0, detail
    assert out.exists(), "probe wrote no output\n" + detail
    return json.loads(out.read_text()), proc


def _assert_throwaway(probe, ambient_dir=None):
    home = probe["engram_home"]
    prefix = os.path.join(os.path.realpath(probe["tempdir"]), THROWAWAY_PREFIX)
    assert home is not None and os.path.realpath(home).startswith(prefix), (
        f"child ENGRAM_HOME {home!r} is not a throwaway under {prefix!r}"
    )
    if ambient_dir is not None:
        assert os.path.realpath(home) != os.path.realpath(str(ambient_dir))
    assert Path(probe["data_dir"]).resolve() == Path(home).resolve(), probe
    # C7: the throwaway is private (0700); the conftest comment's cross-uid
    # collision argument depends on it.
    assert probe["home_mode"] == 0o700, f"child throwaway mode {probe['home_mode']:o}"


def test_c1_exported_ambient_home_is_not_written(tmp_path):
    """C1: ENGRAM_HOME exported to an existing empty dir X -> X stays empty."""
    x = tmp_path / "X"
    x.mkdir()
    probe, _ = _run_child(tmp_path, str(x))
    assert list(x.iterdir()) == [], (
        f"the child wrote into the exported ENGRAM_HOME: {sorted(p.name for p in x.iterdir())}"
    )
    _assert_throwaway(probe, ambient_dir=x)
    assert "error" not in json.loads(probe["result"]), probe["result"]


def test_c2_absent_home_is_throwaway(tmp_path):
    """C2: ENGRAM_HOME absent -> throwaway, as before."""
    probe, _ = _run_child(tmp_path, _UNSET)
    _assert_throwaway(probe)


def test_c3_empty_string_home_is_throwaway(tmp_path):
    """C3: ENGRAM_HOME exported as "" is the ambient case too; it must not
    resolve to ~/.engram."""
    probe, _ = _run_child(tmp_path, "")
    _assert_throwaway(probe)
    assert Path(probe["data_dir"]) != Path(probe["default_home"]), probe
    assert not (tmp_path / "child_home" / ".engram").exists()


def test_c4_ambient_value_is_recorded(tmp_path):
    """C4: _AMBIENT_ENGRAM_HOME is the exact ambient value: X / "" / None."""
    for sub in ("a", "b", "c"):
        (tmp_path / sub).mkdir()
    x = tmp_path / "X"
    x.mkdir()
    probe_x, _ = _run_child(tmp_path / "a", str(x))
    assert probe_x["ambient"] == str(x)
    probe_empty, _ = _run_child(tmp_path / "b", "")
    assert probe_empty["ambient"] == ""
    probe_unset, _ = _run_child(tmp_path / "c", _UNSET)
    assert probe_unset["ambient"] is None


def _find_conftest_module():
    for mod in list(sys.modules.values()):
        if (
            getattr(mod, "__file__", None)
            and os.path.realpath(mod.__file__) == str(CONFTEST_PATH.resolve())
        ):
            return mod
    return None


def test_c5_this_session_runs_in_a_throwaway_home():
    """C5 (#4906 ask 2): in THIS real session ENGRAM_HOME is a throwaway, not
    the ambient value the shell exported."""
    mod = _find_conftest_module()
    assert mod is not None, "tests/conftest.py not found among loaded modules"
    ambient = getattr(mod, "_AMBIENT_ENGRAM_HOME", "<missing>")
    resolved = os.environ.get("ENGRAM_HOME")
    msg = f"resolved ENGRAM_HOME={resolved!r}, ambient={ambient!r}"
    assert ambient != "<missing>", "conftest does not record _AMBIENT_ENGRAM_HOME; " + msg
    prefix = os.path.join(os.path.realpath(tempfile.gettempdir()), THROWAWAY_PREFIX)
    fallback = getattr(mod, "_FALLBACK_ENGRAM_HOME", None)
    assert fallback, "conftest has no _FALLBACK_ENGRAM_HOME; " + msg
    assert os.path.realpath(fallback).startswith(prefix), (
        f"conftest throwaway {fallback!r} is not under {prefix!r}; " + msg
    )
    assert resolved, msg
    # Other test modules may set their own ENGRAM_HOME at import time, so the
    # live value is held only to "under the temp dir, and not the ambient one".
    assert os.path.realpath(resolved).startswith(
        os.path.realpath(tempfile.gettempdir()) + os.sep
    ), msg
    if ambient is not None:
        assert os.path.realpath(resolved) != os.path.realpath(ambient), msg
        assert os.path.realpath(fallback) != os.path.realpath(ambient), msg


def test_c7_throwaway_home_is_private():
    """C7: this session's throwaway home is mode 0700."""
    mod = _find_conftest_module()
    assert mod is not None, "tests/conftest.py not found among loaded modules"
    fallback = getattr(mod, "_FALLBACK_ENGRAM_HOME", None)
    assert fallback, "conftest has no _FALLBACK_ENGRAM_HOME"
    mode = stat.S_IMODE(os.stat(fallback).st_mode)
    assert mode == 0o700, f"{fallback} has mode {mode:o}, expected 700"


def test_c6_counterfactual_file_isolates_what_the_code_reads(monkeypatch, tmp_path):
    """C6 (#4881 pin): the counterfactual file's setup_module() must redirect
    engram_core.DATA_DIR to its TEST_DIR (it set a variable nothing reads)."""
    name = "counterfactual_quote_type_under_test"
    spec = importlib.util.spec_from_file_location(name, COUNTERFACTUAL_PATH)
    cf = importlib.util.module_from_spec(spec)
    sys.modules[name] = cf
    try:
        spec.loader.exec_module(cf)
        c6_home = tmp_path / "c6-home"
        c6_home.mkdir()
        monkeypatch.setenv("ENGRAM_HOME", str(c6_home))
        cf.setup_module()
        try:
            import engram_core

            assert Path(engram_core.DATA_DIR).resolve() == Path(cf.TEST_DIR).resolve(), (
                f"DATA_DIR={engram_core.DATA_DIR!r} is not the file's TEST_DIR={cf.TEST_DIR!r}"
            )
            # fresh_server() deletes TEST_DIR first, so only _ensure_data_dir()
            # brings it back: pins that call as well as _configure_paths().
            assert Path(cf.TEST_DIR).is_dir(), (
                f"{cf.TEST_DIR} was not created: _ensure_data_dir() not called"
            )
        finally:
            cf.teardown_module()
    finally:
        sys.modules.pop(name, None)
