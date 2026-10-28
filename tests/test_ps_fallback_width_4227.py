"""#4227: `ps -p P -o args=` truncates to $COLUMNS (pytest-xdist workers export
COLUMNS=80), so a long install path loses the script token and a ps-based
identity check reads a genuine process as "not a match".  One cell per site that
reads argv through ps: _proc_identity (K2), check_stale_processes, and
codex-umwelt-wake proc_cmdline.  Each cell asserts its own precondition (the
path is long and `COLUMNS=20 ps` really truncates here) and skips with a reason
otherwise, so it can never pass vacuously.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ENGRAM = _REPO_ROOT / "src" / "engram"
_TOOLS = _SRC_ENGRAM / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.append(str(_TOOLS))
if str(_SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(_SRC_ENGRAM))

from _proc_identity import check_process_identity  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _long_script(tmp_path: Path, script: str, body: str) -> Path:
    d = tmp_path / "a-deliberately-long-install-directory" / "nested-more" / "bin"
    d.mkdir(parents=True)
    p = d / script
    p.write_text(body, encoding="utf-8")
    assert len(str(p.parent)) > 20, "path before the script token must exceed 20 columns"
    return p


def _spawn(argv: list[str]) -> subprocess.Popen:
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    assert proc.poll() is None
    return proc


def _require_truncation(pid: int, script: str) -> None:
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "args="],
        capture_output=True,
        text=True,
        env=dict(os.environ, COLUMNS="20"),
        timeout=5,
    ).stdout
    if script in out:
        pytest.skip("ps on this host ignores $COLUMNS; the #4227 precondition cannot be met")


def _kill(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(timeout=5)


def test_proc_identity_ps_fallback_survives_narrow_columns_4227(tmp_path, monkeypatch):
    """K2: ps fallback forced + COLUMNS=20 -> a live `bash <long>/<script>` is rc 3."""
    script = "fake-monitor-4227.sh"
    sh = _long_script(tmp_path, script, "sleep 60\n")
    empty = tmp_path / "empty-proc"
    empty.mkdir()
    proc = _spawn(["bash", str(sh)])
    try:
        _require_truncation(proc.pid, script)
        monkeypatch.setenv("ENGRAM_PROC_DIR", str(empty))
        monkeypatch.setenv("COLUMNS", "20")
        rc, text = check_process_identity(str(proc.pid), script)
        assert rc == 3, (rc, text)  # text is cut to ~100 chars by design, so do not assert on it
    finally:
        _kill(proc)


def test_check_stale_processes_ps_argv_survives_narrow_columns_4227(tmp_path, monkeypatch):
    """Sibling: check_stale_processes._real_argv via its ps branch under COLUMNS=20."""
    tool = _load("check_stale_processes_4227", _TOOLS / "check_stale_processes.py")
    script = "engramd_owner.py"
    p = _long_script(tmp_path, script, "import time\ntime.sleep(60)\n")
    proc = _spawn([sys.executable, str(p)])
    try:
        _require_truncation(proc.pid, script)
        monkeypatch.setattr(tool, "_have_proc", lambda: False)
        monkeypatch.setenv("COLUMNS", "20")
        argv = tool._real_argv(proc.pid)
        assert argv is not None and tool.identity_ok(argv, script), argv
    finally:
        _kill(proc)


def test_codex_wake_proc_cmdline_survives_narrow_columns_4227(tmp_path, monkeypatch):
    """Sibling: codex-umwelt-wake.proc_cmdline keeps the script token under COLUMNS=20."""
    wake = _load("codex_umwelt_wake_4227", _TOOLS / "codex-umwelt-wake.py")
    script = "codex-umwelt-wake.py"
    p = _long_script(tmp_path, script, "import time\ntime.sleep(60)\n")
    proc = _spawn([sys.executable, str(p)])
    try:
        _require_truncation(proc.pid, script)
        monkeypatch.setenv("COLUMNS", "20")
        status, cmd = wake.proc_cmdline(proc.pid)
        assert status == "ok"
        assert wake._is_our_process(cmd), cmd
    finally:
        _kill(proc)
