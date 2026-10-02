"""The forum installer must ship ``baton_gate.py`` (#3039 U2b merge-seat ruling).

``server._load_baton_gate()`` walk-parents-searches for ``tools/baton_gate.py``.
``install-forum-service.sh`` snapshots only ``src/forum/`` and
``tools/engram-pkg/`` into ``$APP``, so a fresh install had NO ``$APP/tools/
baton_gate.py`` and the loader returned None: every PR-anchored flip, and (since
the phase-injection fix) every claim/release, would fail closed. The live host
only worked because a copy was hand-placed.

Contract:
  * ``--dry-run`` announces the snapshot of ``baton_gate.py`` into
    ``$APP/tools/baton_gate.py``.
  * The REAL (non dry-run) copy line resolves the symlink (``tools/baton_gate.py``
    is a symlink to ``src/engram/tools/baton_gate.py``): the installed file is a
    regular file with the target's bytes, not a dangling link.
  * In the installed layout (``$APP/forum/`` + ``$APP/tools/baton_gate.py``),
    ``forum.server._load_baton_gate()`` resolves a module FROM that file and
    does not return None.

The real installer cannot be run here (it needs systemctl, a venv build and
group membership), so the real copy line is extracted from the script and
executed on its own against a scratch ``$SRC`` / ``$APP``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INSTALLER = _REPO_ROOT / "src" / "forum" / "deploy" / "install-forum-service.sh"
_GATE_REAL = _REPO_ROOT / "src" / "engram" / "tools" / "baton_gate.py"

# The executable (non-echo) copy line the installer must contain.
_COPY_LINE_RE = re.compile(
    r'^\s*cp -L "\$SRC/tools/baton_gate\.py" "\$APP/tools/baton_gate\.py"\s*$', re.M)


def _dry_run(tmp_path: Path) -> str:
    r = subprocess.run(
        ["bash", str(_INSTALLER), "--src", str(_REPO_ROOT),
         "--service-dir", str(tmp_path / "forum"), "--dry-run"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout + r.stderr


def _install_copy_step(tmp_path: Path) -> Path:
    """Run the installer's own REAL copy line for baton_gate.py into a scratch $APP."""
    m = _COPY_LINE_RE.search(_INSTALLER.read_text())
    assert m, "install-forum-service.sh has no real `cp -L ... baton_gate.py` line"
    app = tmp_path / "svc" / "app"
    script = f'set -e\nSRC="{_REPO_ROOT}"\nAPP="{app}"\nmkdir -p "$APP/tools"\n{m.group(0).strip()}\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return app


def _snapshot_forum_package(app: Path) -> None:
    """What the installer's rsync does: src/forum -> $APP/forum minus non-runtime."""
    shutil.copytree(
        _REPO_ROOT / "src" / "forum", app / "forum",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "deploy",
                                      "fairy-spec-*.md", "spec.md", "README.md"))
    (app / "forum" / "__init__.py").touch()


def _gate_file_seen_by_installed_server(app: Path):
    """Run the INSTALLED server's loader in a fresh interpreter (no module cache)."""
    code = textwrap.dedent("""
        import forum.server as s
        m = s._load_baton_gate()
        print("NONE" if m is None else m.__file__)
    """)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(app),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_dry_run_announces_the_baton_gate_snapshot(tmp_path):
    out = _dry_run(tmp_path)
    line = next((ln for ln in out.splitlines() if "baton_gate.py" in ln and "dry-run" in ln), None)
    assert line is not None, out
    assert str(tmp_path / "forum" / "app" / "tools" / "baton_gate.py") in line


def test_real_copy_line_resolves_the_symlink(tmp_path):
    assert (_REPO_ROOT / "tools" / "baton_gate.py").is_symlink(), "fixture assumption"
    app = _install_copy_step(tmp_path)
    dest = app / "tools" / "baton_gate.py"
    assert dest.is_file() and not dest.is_symlink()
    assert dest.read_bytes() == _GATE_REAL.read_bytes()


def test_installed_layout_loads_the_gate_from_app_tools(tmp_path):
    app = _install_copy_step(tmp_path)
    _snapshot_forum_package(app)
    seen = _gate_file_seen_by_installed_server(app)
    assert seen != "NONE", "installed server could not load baton_gate (fail-closed on every flip)"
    assert Path(seen).resolve() == (app / "tools" / "baton_gate.py").resolve()


def test_control_layout_without_the_installer_copy_cannot_load_the_gate(tmp_path):
    """Proves the layout test above can fail: the pre-fix installed tree."""
    app = tmp_path / "svc" / "app"
    app.mkdir(parents=True)
    _snapshot_forum_package(app)
    assert _gate_file_seen_by_installed_server(app) == "NONE"
