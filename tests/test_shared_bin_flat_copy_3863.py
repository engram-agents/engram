"""#3863 PR 6b: ia and forum run as FLAT COPIES in /home/agents-shared/bin (measured on the host:
regular files with only forum_api.py and _status_derive.py beside them), so they must resolve the ENGRAM
home through forum_api's pinned copy and never import `_owner_read` or any plugin-tree module.

Each cell copies ONLY the files that sit beside the shared-bin copies into an empty directory and imports
from there in a child python with a hard timeout (stdlib only, no plugin tree on sys.path).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "src" / "engram" / "tools"
FLAT = ("forum_api.py", "_status_derive.py")


@pytest.fixture
def flat(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    shutil.copy(TOOLS / "ia.py", d / "ia.py")
    shutil.copy(TOOLS / "forum.py", d / "forum.py")
    for f in FLAT:
        shutil.copy(TOOLS / f, d / f)
    return d


def _run(flat: Path, code: str, env_home, home: Path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    if env_home is not None:
        env["ENGRAM_HOME"] = env_home
    return subprocess.run([sys.executable, "-c", code], cwd=str(flat), env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("mod", ["ia", "forum", "_status_derive"])
def test_f1_imports_and_follows_engram_home_from_a_flat_copy(flat, tmp_path, mod):
    custom = tmp_path / "my engram's home"
    r = _run(flat, f"import {mod}; print({mod}.ENGRAM_HOME)", str(custom), tmp_path / "h")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(custom)


@pytest.mark.parametrize("mod", ["ia", "forum", "_status_derive"])
@pytest.mark.parametrize("how", ["unset", "empty"])
def test_f2_unset_and_empty_mean_home_dot_engram(flat, tmp_path, mod, how):
    home = tmp_path / "h"
    home.mkdir()
    r = _run(flat, f"import {mod}; print({mod}.ENGRAM_HOME)", None if how == "unset" else "", home)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(home / ".engram")


@pytest.mark.parametrize("mod", ["ia", "forum", "_status_derive"])
def test_f3_no_plugin_tree_module_is_imported(flat, tmp_path, mod):
    code = (f"import sys, {mod}; "
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith(('tools', '_owner_read', 'engramd', 'engram_')))))"
            ).replace("print(", "import json; print(", 1)
    r = _run(flat, code, str(tmp_path / "x"), tmp_path / "h")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == []


def test_f4_the_shared_bin_files_do_not_import_the_tools_layer_resolver():
    import ast
    for f in ("ia.py", "forum.py", "_status_derive.py", "forum_api.py"):
        tree = ast.parse((TOOLS / f).read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                names.add(n.module or "")
                names.update(a.name for a in n.names)
        bad = {x for x in names if "_owner_read" in x or x == "tools" or x.startswith("tools.")}
        assert not bad, f"{f} imports {sorted(bad)}; flat copies cannot (only forum_api and _status_derive sit beside them)"
