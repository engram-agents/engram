"""Regression pin: the process-cached telemetry file path must not leak across tests (#3539).

`engram_telemetry_store._PROCESS_FILE_PATH` is resolved once per process. Under
pytest, the first test that emits telemetry fixes it inside ITS OWN tmp
ENGRAM_HOME; every later test then wrote into that directory. With the default
`tmp_path_retention_policy=all` the directory survived (a silent cross-test
leak). With `=failed` (the nightly's setting) it is deleted at that test's
teardown, so later writes raised FileNotFoundError to stderr and tripped
`assert err == ""` in tests/test_route_ledger.py.

The tests/conftest.py autouse fixture `_reset_telemetry_process_file_path`
resets the cache before each test. This test runs the exact repro in a
subprocess (the retention policy is a session-level option, so it cannot be
flipped in-process).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Run as a PAIR: the first test fixes the cached path in its own tmp home, the
# second then writes under a new ENGRAM_HOME. The pair also keeps the one-shot
# "sqlite-vec unavailable" stderr notice inside test 1, so the second test's
# `assert err == ""` is not tripped by it.
PAIR = [
    "tests/test_route_ledger.py::test_surface_no_probe_dir_writes_nothing",
    "tests/test_route_ledger.py::test_surface_session_dir_absent_but_other_session_present",
]


def test_telemetry_path_does_not_leak_across_tests_under_failed_retention():
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env["ENGRAM_NO_EMBEDDINGS"] = "1"
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", *PAIR,
            "-o", "tmp_path_retention_policy=failed",
            "-p", "no:cacheprovider",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert "FileNotFoundError" not in out, out[-4000:]
    assert proc.returncode == 0, out[-4000:]
