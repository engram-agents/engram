#!/usr/bin/env python3
"""Pre-deploy check — runs pytest against tests/.

Exit codes:
    0   all tests passed, safe to start the server
    2   pytest is not installed in this interpreter; nothing ran
    other  pytest's own exit code (test failures, collection errors, etc.)

Usage:
    python3 tools/selftest.py            # run full suite
    python3 tools/selftest.py --smoke    # quieter output (-q) for quick confidence check
    python3 tools/selftest.py --quiet    # short summary only

Migrated from monolithic test_full_lifecycle.py to pytest 2026-05-06.
Tests live under tests/. CI runs the same invocation via
.github/workflows/tests.yml on PRs to dev / main.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path


def _pytest_available() -> bool:
    try:
        return importlib.util.find_spec("pytest") is not None
    except (ValueError, ModuleNotFoundError):
        return False


def main():
    if not _pytest_available():
        print(
            "ERROR: pytest is not installed in this Python environment.\n"
            f"  Interpreter: {sys.executable}\n"
            "\n"
            "Install it before running selftest:\n"
            "  pip install pytest\n"
            "\n"
            "If you use a virtual environment, activate it first — selftest\n"
            "runs against whichever interpreter invoked it.",
            file=sys.stderr,
        )
        sys.exit(2)

    here = str(Path(__file__).resolve().parent.parent)
    extra = sys.argv[1:]

    if "--smoke" in extra or "--quiet" in extra:
        # Both flags map to pytest's -q; older callers used distinct meanings,
        # but for a wrapper around pytest the verbosity collapse is harmless.
        extra = [a for a in extra if a not in ("--smoke", "--quiet")]
        extra.insert(0, "-q")

    cmd = [sys.executable, "-m", "pytest", "tests/", *extra]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    sys.exit(subprocess.run(cmd, cwd=here).returncode)


if __name__ == "__main__":
    main()
