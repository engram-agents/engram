"""#3225 — conftest restores the ``fresh_module`` sys.modules purge at file end.

``fresh_module`` deletes every ``server`` / ``engram_*`` entry from
``sys.modules`` and re-imports.  Without a restore, a LATER test file that bound
one of those modules at import time patches an orphaned object.  The fix is an
autouse *module-scoped* fixture in ``tests/conftest.py``.

Harness choice: ``pytester`` is NOT enabled in this repo (no ``-p pytester`` in
``pytest.ini`` / conftest), so each case runs a nested ``python -m pytest`` in a
subprocess over two tiny generated files in ``tmp_path``.  The generated
``conftest.py`` loads THIS repo's ``tests/conftest.py`` by path and re-exports
its names, so the fixture under test is the real one.  Everything is exercised
on synthetic ``engram_zz_*_3225`` modules so the result does not depend on which
real modules happen to be loaded.

Contract:
  R1  a module purged in file A is back to its original object for file B.
  R2  within file A, a ``fresh_module`` result stays fresh across that file's
      tests -- both from a module-scoped setup fixture and from a call made in a
      test body (guards against a function-scoped restore; note that a
      module-scoped setup alone does NOT catch one, because higher-scoped
      fixtures are set up before the function-scoped snapshot is taken).
  R3  an ``engram_*`` module first imported in file A is absent after A.
"""

import importlib.util
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

_REPO_CONFTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py")

_CONFTEST = f"""
import importlib.util, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("_repo_conftest_3225", {_REPO_CONFTEST!r})
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
globals().update({{k: v for k, v in vars(_mod).items() if not k.startswith("__")}})
"""

_PROBE = "MARKER = 'probe'\n"
_NEW = "MARKER = 'new'\n"

_FILE_A = """
import importlib
import sys

import pytest
import engram_zz_probe_3225 as probe
from conftest import fresh_module

_STATE = {}


@pytest.fixture(scope="module")
def fresh():
    _STATE["fresh"] = fresh_module(probe)
    return _STATE["fresh"]


def test_a1_purge_happened(fresh):
    assert fresh is not probe
    assert sys.modules["engram_zz_probe_3225"] is fresh


def test_a2_intra_file_freshness_preserved(fresh):  # R2
    assert sys.modules["engram_zz_probe_3225"] is _STATE["fresh"]
    assert sys.modules["engram_zz_probe_3225"] is not probe


def test_a2b_purge_inside_test_body():
    # Called in a test body (function scope), so a function-scoped restore
    # would undo it at this test's teardown -- see test_a2c below.
    _STATE["body"] = fresh_module(probe)
    assert _STATE["body"] is not probe


def test_a2c_body_purge_survives_to_next_test():  # R2 (function-scope guard)
    assert sys.modules["engram_zz_probe_3225"] is _STATE["body"]


def test_a3_import_new_module():
    importlib.import_module("engram_zz_new_3225")
    assert "engram_zz_new_3225" in sys.modules
"""

_FILE_B = """
import sys

import engram_zz_probe_3225 as probe_b  # bound at import (collection) time

_ORIGINAL = probe_b


def test_b1_original_restored():  # R1
    assert sys.modules["engram_zz_probe_3225"] is _ORIGINAL


def test_b2_new_key_removed():  # R3
    assert "engram_zz_new_3225" not in sys.modules
"""


def _run_pair(tmp_path):
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "engram_zz_probe_3225.py").write_text(_PROBE)
    (tmp_path / "engram_zz_new_3225.py").write_text(_NEW)
    (tmp_path / "test_a_3225.py").write_text(textwrap.dedent(_FILE_A))
    (tmp_path / "test_b_3225.py").write_text(textwrap.dedent(_FILE_B))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    env.pop("PYTEST_ADDOPTS", None)
    report = tmp_path / "report.xml"
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
           "--rootdir", str(tmp_path), "-c", os.devnull,
           f"--junitxml={report}"]
    if importlib.util.find_spec("pytest_randomly") is not None:
        cmd += ["-p", "no:randomly"]
    cmd += ["test_a_3225.py", "test_b_3225.py"]
    proc = subprocess.run(
        cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    # Outcomes come from the junit XML, never from terminal text (ANSI colour
    # from FORCE_COLOR & co. corrupts a scraped -v log).  No report (collection
    # error, crash) must fail loudly, not read as "no tests".
    # rc 0 = all passed, 1 = some tests failed (both leave a usable report);
    # rc 2+ = interrupted / collection error / usage error, where pytest may
    # still write a report containing only the collection error.
    assert report.exists() and proc.returncode in (0, 1), (
        f"nested pytest did not run cleanly (rc={proc.returncode}, "
        f"report written: {report.exists()})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    outcomes = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        if case.find("failure") is not None:
            verdict = "FAILED"
        elif case.find("error") is not None:
            verdict = "ERROR"
        elif case.find("skipped") is not None:
            verdict = "SKIPPED"
        else:
            verdict = "PASSED"
        outcomes[case.get("name")] = verdict
    return outcomes, proc


def _assert_outcome(tmp_path, *test_names):
    outcomes, proc = _run_pair(tmp_path)
    for test_name in test_names:
        assert outcomes.get(test_name) == "PASSED", (
            f"{test_name}: {outcomes.get(test_name)!r}\n"
            f"outcomes={outcomes}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


def test_r1_purged_module_restored_for_next_file(tmp_path):
    _assert_outcome(tmp_path, "test_b1_original_restored")


def test_r2_intra_file_freshness_preserved(tmp_path):
    _assert_outcome(
        tmp_path,
        "test_a2_intra_file_freshness_preserved",
        "test_a2c_body_purge_survives_to_next_test",
    )


def test_r3_new_engram_module_removed_after_file(tmp_path):
    _assert_outcome(tmp_path, "test_b2_new_key_removed")


def test_purge_and_restore_share_one_predicate(tmp_path, monkeypatch):
    """The purge in ``fresh_module`` and the restore fixture use ONE predicate.

    If ``fresh_module`` keeps an inline copy of the family test, the two can
    drift and the restore silently stops covering what the purge removes.
    Widen ``_is_family_module_key`` to also match a synthetic non-engram name;
    ``fresh_module`` must then purge that name too.  Runs in-process on a copy
    of conftest loaded by path (as ``_CONFTEST`` does); ``sys.modules`` is
    restored in ``finally`` because the purge is process-wide.
    """
    import importlib.util
    import types

    spec = importlib.util.spec_from_file_location("_repo_conftest_3225_pred", _REPO_CONFTEST)
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)

    (tmp_path / "engram_zz_target_3225.py").write_text(_PROBE)
    monkeypatch.syspath_prepend(str(tmp_path))
    original = cf._is_family_module_key
    monkeypatch.setattr(
        cf, "_is_family_module_key",
        lambda key: original(key) or key == "zz_probe_3225",
    )
    saved = dict(sys.modules)
    try:
        import engram_zz_target_3225 as target

        sys.modules["zz_probe_3225"] = types.ModuleType("zz_probe_3225")
        cf.fresh_module(target)
        purged = "zz_probe_3225" not in sys.modules
    finally:
        sys.modules.clear()
        sys.modules.update(saved)
    assert purged, "fresh_module's purge ignored _is_family_module_key"


def test_outcome_parse_is_immune_to_forced_color(tmp_path, monkeypatch):
    """Outcomes must not be scraped from terminal text (ANSI-colour immune).

    A local shell with ``FORCE_COLOR`` set colours the nested run's ``-v``
    output and broke a terminal-text parse (``outcomes={}``); CI does not set
    it, so only local runs saw the failure.  The helper reads a junit XML
    report instead, so forcing colour in every common way must not matter.
    """
    for var in ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE"):
        monkeypatch.setenv(var, "1")
    outcomes, _proc = _run_pair(tmp_path)
    assert outcomes.get("test_b1_original_restored") == "PASSED", outcomes
    assert outcomes.get("test_b2_new_key_removed") == "PASSED", outcomes
    assert outcomes.get("test_a2c_body_purge_survives_to_next_test") == "PASSED", outcomes


def test_run_pair_fails_loudly_when_nested_run_has_no_report(tmp_path, monkeypatch):
    """A nested run that cannot run cleanly must raise WITH its output.

    A collection/syntax error yields no (or only an error-only) junit XML and
    rc 2.  Returning ``{}`` or a lone collection ERROR would read
    as "no tests" and every ``_assert_outcome`` would fail with an unhelpful
    ``outcomes={}``; the helper must instead surface the nested stderr/stdout.
    """
    import pytest

    monkeypatch.setitem(globals(), "_FILE_A", "def broken(:\n")
    with pytest.raises(AssertionError) as excinfo:
        _run_pair(tmp_path)
    msg = str(excinfo.value)
    assert "SyntaxError" in msg, msg
    assert "outcomes={}" not in msg, msg
