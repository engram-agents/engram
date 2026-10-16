"""#3784 (b) -- tests/conftest.py pre-imports ``tracemalloc`` at module level.

Why the import exists: pytest's ``thread_exception_hook`` imports ``tracemalloc``
lazily, for the first time, to format a thread-exception report.  If another
thread clears ``sys.modules`` mid-import (``mock.patch.dict(sys.modules, ...)``
does ``clear()`` then ``update(snapshot)`` on exit), the half-imported module's
entry vanishes and importlib's ``_load_unlocked`` dies with ``KeyError``.  That
KeyError is the SECOND failure and masks the ORIGINAL thread exception.
Importing ``tracemalloc`` at conftest load puts it in every ``patch.dict``
snapshot, closing the window.

Contract:
  C1  after conftest load, at collection, ``"tracemalloc" in sys.modules``
      (nested session, so an import placed in a fixture or function is red).
  C3  the mechanism, in-process, both directions:
        control -- tracemalloc already imported: a thread import overlapping a
                   ``patch.dict(sys.modules)`` exit does not raise;
        race    -- a module whose import starts inside the block and outlives
                   it raises ``KeyError`` in the importing thread.
"""

import importlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from unittest import mock

_REPO_CONFTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py")

_CONFTEST = f"""
import importlib.util, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("_repo_conftest_3784", {_REPO_CONFTEST!r})
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
globals().update({{k: v for k, v in vars(_mod).items() if not k.startswith("__")}})
"""

# The assertion runs at MODULE level, i.e. at collection: after conftest load,
# before any fixture or test body.
_PROBE_FILE = """
import sys

assert "tracemalloc" in sys.modules, "tracemalloc not imported at conftest load"


def test_probe_collected_and_passes():
    assert "tracemalloc" in sys.modules
"""


def _run_nested(tmp_path):
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_probe_3784.py").write_text(_PROBE_FILE)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    env.pop("PYTEST_ADDOPTS", None)
    report = tmp_path / "report.xml"
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
           "--rootdir", str(tmp_path), "-c", os.devnull, f"--junitxml={report}"]
    if importlib.util.find_spec("pytest_randomly") is not None:
        cmd += ["-p", "no:randomly"]
    cmd += ["test_probe_3784.py"]
    proc = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=120)
    outcomes = {}
    if report.exists():
        for case in ET.parse(report).getroot().iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                outcomes[case.get("name")] = "FAILED"
            elif case.find("skipped") is not None:
                outcomes[case.get("name")] = "SKIPPED"
            else:
                outcomes[case.get("name")] = "PASSED"
    return outcomes, proc


def test_c1_tracemalloc_imported_at_conftest_load(tmp_path):
    outcomes, proc = _run_nested(tmp_path)
    assert proc.returncode == 0 and outcomes == {"test_probe_collected_and_passes": "PASSED"}, (
        f"rc={proc.returncode} outcomes={outcomes}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


def _import_in_thread(name, started):
    """Run ``import_module(name)`` in a thread; return (thread, result dict)."""
    result = {}

    def run():
        started.set()
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - the exception IS the result
            result["exc"] = exc

    return threading.Thread(target=run), result


def test_c3_control_preimported_module_survives_patch_dict_exit():
    importlib.import_module("tracemalloc")  # control precondition: in snapshot
    started = threading.Event()
    t, result = _import_in_thread("tracemalloc", started)
    with mock.patch.dict(sys.modules, {"zz_unused_3784": object()}):
        t.start()
        assert started.wait(5)
        t.join(5)
    assert not t.is_alive()
    assert "exc" not in result, result.get("exc")
    assert "tracemalloc" in sys.modules


def test_c3_race_import_outliving_patch_dict_exit_raises_keyerror(tmp_path, monkeypatch):
    name = "zz_slow_import_3784"
    (tmp_path / f"{name}.py").write_text("import time\ntime.sleep(0.5)\nVALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop(name, None)
    started = threading.Event()
    t, result = _import_in_thread(name, started)
    try:
        with mock.patch.dict(sys.modules, {"zz_unused_3784": object()}):
            t.start()
            assert started.wait(5)
            # Wait until the import is in flight (entry registered, body
            # sleeping), then leave the block while it is still running.
            deadline = time.monotonic() + 5
            while name not in sys.modules and time.monotonic() < deadline:
                time.sleep(0.005)
            assert name in sys.modules, "import never started inside the block"
    finally:
        t.join(10)
        sys.modules.pop(name, None)
    assert not t.is_alive()
    exc = result.get("exc")
    assert isinstance(exc, KeyError) and exc.args == (name,), repr(exc)
