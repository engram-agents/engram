#!/usr/bin/env python3
"""Pre-deploy check — runs pytest against tests/.

Exit code 0 = all tests passed, safe to start the server.
Exit code != 0 = failures detected, investigate before deploying.

Usage:
    python3 selftest.py            # run full suite
    python3 selftest.py --smoke    # quieter output (-q) for quick confidence check
    python3 selftest.py --quiet    # short summary only

Migrated from monolithic test_full_lifecycle.py to pytest 2026-05-06.
Tests live under tests/. CI runs the same invocation via
.github/workflows/tests.yml on PRs to dev / master.
"""

import os
import subprocess
import sys


def main():
    here = os.path.dirname(os.path.abspath(__file__))
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
