"""Contract tests for alpha #3335: collecting tests/test_forum_db_hostname.py must not
clobber `forum` modules that earlier-collected test files already hold, while it must
still shed a SHADOWED `forum` (tools/forum.py resolved ahead of the src/forum package,
the #2063/#2064 defect).

Written BEFORE the fix. Both cells run pytest in a SUBPROCESS: the defect is a
collection-time side effect on `sys.modules`, so it only shows when the two files are
collected in one process.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(args, *, code=None):
    cmd = [sys.executable] + (["-c", code] if code else ["-m", "pytest"] + args)
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=300)


def test_collecting_the_hostname_file_after_test_packs_does_not_break_test_packs():
    """The minimal pair measured on origin/dev (alpha #3335): 3 failed / 61 passed
    (TestMissingValidatorReturns503 returns 201, not 503). Mutation: restore the
    unconditional `del sys.modules[...]` loop in tests/test_forum_db_hostname.py -> red."""
    r = _run(["src/forum/tests/test_packs.py", "tests/test_forum_db_hostname.py",
              "-q", "-p", "no:cacheprovider"])
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def test_the_hostname_file_still_recovers_from_a_shadowed_forum_module():
    """A `forum` cached from tools/forum.py (the #2063 shadow) must not survive into
    the hostname file's tests. Pre-import the shadow in the process, then run the
    hostname file in that same process. Mutation: remove the stale-module handling
    entirely -> `from forum import db` fails ("cannot import name 'db' from 'forum'")
    -> red."""
    code = (
        "import sys, importlib.util\n"
        "from pathlib import Path\n"
        "repo = Path.cwd()\n"
        "spec = importlib.util.spec_from_file_location('forum', repo / 'tools' / 'forum.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "sys.modules['forum'] = mod  # the cached shadow\n"
        "assert Path(sys.modules['forum'].__file__).resolve() == (repo / 'tools' / 'forum.py').resolve()\n"
        "import pytest\n"
        "sys.exit(pytest.main(['tests/test_forum_db_hostname.py', '-q', '-p', 'no:cacheprovider']))\n"
    )
    r = _run(None, code=code)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def test_a_shadow_in_a_directory_merely_named_forum_is_still_shed():
    """Pins the path rule itself (reviewer-fairy, PR #3337): the real package is
    recognised by its file being UNDER <repo>/src/forum/, not by its parent
    directory being called 'forum'. A cached `forum` whose file lives in some
    other directory named forum (a different tree, a vendored copy) is a shadow.
    Mutation: compare `Path(__file__).parent.name == 'forum'` instead of the path
    containment test -> this shadow is kept -> `from forum import db` binds the
    wrong object / fails -> red."""
    code = (
        "import sys, tempfile, importlib.util\n"
        "from pathlib import Path\n"
        "repo = Path.cwd()\n"
        "d = Path(tempfile.mkdtemp()) / 'forum'\n"
        "d.mkdir()\n"
        "(d / '__init__.py').write_text('SHADOW = True\\n')\n"
        "spec = importlib.util.spec_from_file_location('forum', d / '__init__.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "sys.modules['forum'] = mod\n"
        "assert Path(mod.__file__).parent.name == 'forum'\n"
        "import pytest\n"
        "sys.exit(pytest.main(['tests/test_forum_db_hostname.py', '-q', '-p', 'no:cacheprovider']))\n"
    )
    r = _run(None, code=code)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]
