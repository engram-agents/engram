"""engram-snapshot resolves its home through the shared tools resolver [#3863].

Contract (C5): with ENGRAM_HOME=/abs/dir the script uses /abs/dir; with it
unset it uses the resolver's default; with a RELATIVE value it fails with one
clean `engram-snapshot:` line and exit 1 (the old inline `or Path.home() / ".engram"` accepted a
relative value as-is).
"""

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_snapshot():
    sys.modules.pop("engram_snapshot_home_3863", None)
    loader = importlib.machinery.SourceFileLoader(
        "engram_snapshot_home_3863", str(_TOOLS_DIR / "engram-snapshot")
    )
    spec = importlib.util.spec_from_loader("engram_snapshot_home_3863", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_absolute_engram_home_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_EVIDENCE_ARCHIVE", raising=False)
    mod = _load_snapshot()
    assert mod.ENGRAM_HOME == str(tmp_path)
    assert mod.EVIDENCE_ARCHIVE_ROOT == str(tmp_path / "evidence-archive")


def test_unset_engram_home_uses_the_resolvers_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    monkeypatch.delenv("ENGRAM_EVIDENCE_ARCHIVE", raising=False)
    monkeypatch.syspath_prepend(str(_TOOLS_DIR))
    import _owner_read

    mod = _load_snapshot()
    assert mod.ENGRAM_HOME == str(_owner_read.engram_home())


def test_relative_engram_home_is_refused_with_one_clean_line_and_exit_1(monkeypatch):
    """A relative ENGRAM_HOME exits 1 with ONE `engram-snapshot:` stderr line
    (the script's EXIT_VALIDATION style), not a ValueError traceback."""
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "ENGRAM_HOME"}
    env["ENGRAM_HOME"] = "relative/dir"
    proc = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "engram-snapshot"), "--help"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert "Traceback" not in proc.stderr and "ValueError" not in proc.stderr, proc.stderr
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith("engram-snapshot: ENGRAM_HOME must be an absolute path"), lines[0]
