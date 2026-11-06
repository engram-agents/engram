"""The repo's pytest.ini drops a passing test's tmp_path dir and keeps a failing one's (#4392).

nightly.yml passes the same policy with -o; setting it in pytest.ini too means a
local, merge-seat or pr-checks run behaves like the nightly instead of hiding
tmp-dir-lifetime bugs. Behavioural: runs pytest on a throwaway file with this
repo's pytest.ini via -c and NO -o.
"""
import os
import subprocess
import sys
from pathlib import Path

INI = Path(__file__).resolve().parent.parent / "pytest.ini"

_GENERATED = '''\
def test_passes(tmp_path):
    (tmp_path / "marker.txt").write_text("x")


def test_fails(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    assert False
'''


def test_repo_ini_keeps_only_failing_tests_tmp_dirs(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_gen.py").write_text(_GENERATED, encoding="utf-8")
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env.update({"TMPDIR": str(tmpdir), "TMP": str(tmpdir), "TEMP": str(tmpdir)})
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "-c", str(INI), "--rootdir", str(proj), str(proj / "test_gen.py")],
        cwd=proj, env=env, capture_output=True, text=True, timeout=120,
    )
    assert "1 failed, 1 passed" in r.stdout, r.stdout + r.stderr
    markers = sorted(p.parent.name for p in tmpdir.rglob("marker.txt"))
    assert markers == ["test_fails0"], (markers, r.stdout)
