"""#4834: tests/test_engram_pidfile_identity_3240.py must end clean when it is the
first module in its process to load the surface-daemon hook.

Run alone it ended `29 passed, 1 error` on Linux (dev db4eefc0, 9a8590da, 2afe8959,
320df56c): the #4029 teardown guard in tests/conftest.py fired because
`_load_daemon_module()` executes the hook's module-level `sys.path.insert` in the
pytest process and nothing put it back. A whole-suite run hides it when an earlier
module already loaded the hook. A fresh process removes the order, so the module's
own window is what this cell tests.

Bound: both in-process loads sit in `@linux_only` tests, so on macOS the loads are
skipped and this cell passes with or without the window; it can only go red on
Linux. Mutation: drop the module's `pytestmark` window -> red on Linux (`1 error`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE = "tests/test_engram_pidfile_identity_3240.py"


def test_pidfile_identity_module_alone_has_a_clean_teardown():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", MODULE, "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
