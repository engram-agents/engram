"""Contract tests for the autouse env-restore fixture in tests/conftest.py (#3328).

Contract (L1): after ANY test, os.environ for the keys ENGRAM_HOME,
ENGRAM_SESSION_ROLE and ENGRAM_RUNTIME_DIR (present-or-absent) equals what it
was before that test.  Twelve tests/test_session_start_hook_*.py helpers write
os.environ["ENGRAM_HOME"] directly and never restore it; a long leaked value
changes _hooklib's module-load-time overflow placeholder and flips
tests/test_hook_injection_bounds.py::TestRealOverflowPathRevalidation.

Ordering guarantee: each pair's tests run in definition order, which is
pytest's default within a class (no reordering plugin is configured in this
repo).  The second test of a pair also FAILS (not skips) if the first did not
run, so a reordering plugin cannot make it pass vacuously.

The ABSENT-before cell cannot be guaranteed in-process (a developer shell may
export the keys), so test_absent_keys_stay_absent re-runs TestLeakPair in a
subprocess with the keys stripped from its environment.
"""
import os
import subprocess
import sys
from pathlib import Path

KEYS = ("ENGRAM_HOME", "ENGRAM_SESSION_ROLE", "ENGRAM_RUNTIME_DIR")
_EXPECT_ABSENT = "ENGRAM_CONFTEST_RESTORE_EXPECT_ABSENT"
# Recorded by the first test of a pair: {key: value-or-None} at test start.
_PRE = {}


class TestLeakPair:
    def test_1_leak_the_way_the_helpers_do(self):
        _PRE.clear()
        _PRE.update({k: os.environ.get(k) for k in KEYS})
        if os.environ.get(_EXPECT_ABSENT):
            assert _PRE["ENGRAM_SESSION_ROLE"] is None
            assert _PRE["ENGRAM_RUNTIME_DIR"] is None
        # Direct writes, no monkeypatch -- exactly what the helpers do.
        os.environ["ENGRAM_HOME"] = "/tmp/" + "x" * 150
        os.environ["ENGRAM_SESSION_ROLE"] = "leaked-role"
        os.environ["ENGRAM_RUNTIME_DIR"] = "/tmp/leaked-runtime"

    def test_2_prior_values_intact(self):
        assert _PRE, "test_1 did not run first; ordering assumption violated"
        for k in KEYS:
            assert os.environ.get(k) == _PRE[k], k
        if os.environ.get(_EXPECT_ABSENT):
            assert "ENGRAM_SESSION_ROLE" not in os.environ
            assert "ENGRAM_RUNTIME_DIR" not in os.environ

    def test_3_pop_the_keys(self):
        """effective_role's helper pops ENGRAM_RUNTIME_DIR; pop all three."""
        _PRE.clear()
        _PRE.update({k: os.environ.get(k) for k in KEYS})
        for k in KEYS:
            os.environ.pop(k, None)

    def test_4_popped_values_back(self):
        assert _PRE, "test_3 did not run first; ordering assumption violated"
        for k in KEYS:
            assert os.environ.get(k) == _PRE[k], k


def test_absent_keys_stay_absent():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ENGRAM_SESSION_ROLE", "ENGRAM_RUNTIME_DIR")
    }
    env[_EXPECT_ABSENT] = "1"
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--color=no", f"{Path(__file__).resolve()}::TestLeakPair",
        ],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "4 passed" in proc.stdout, proc.stdout
