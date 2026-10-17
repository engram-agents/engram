"""engram-alpha#3838 -- engramd_owner._read_install_tier must never block on a non-regular config.json.

`EngramdOwner.start()` calls `_read_install_tier` before the owner serves. A plain `Path.read_text` opens a
FIFO blocking until a writer appears (the #3620 class), so a FIFO at `<ENGRAM_HOME>/config.json` hung the
owner's start. The tree's non-blocking reader is `engram_regular_io.read_regular_text` (#3591/#3628): it
raises OSError for a FIFO or a directory instead of waiting, and `_read_install_tier` already maps OSError
to None.

Contract (each cell red against the unchanged code):
  C1  a FIFO at config.json: the call RETURNS None (does not hang). The call runs in a subprocess with a hard
      timeout, so the red state is a clean failure here and not a hung test run.
  C2  a directory at config.json: None.
  C3  the unchanged behaviour on regular files: a string tier is returned; a non-string tier, a JSON list,
      malformed JSON and an absent file give None.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = str(REPO_ROOT / "src" / "engram")
TIMEOUT_SECONDS = 10

_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, {src!r})
    import engramd_owner
    print(repr(engramd_owner._read_install_tier(Path(sys.argv[1]))))
    """
).format(src=SRC)


def _read_tier(home: Path) -> str:
    """-> the repr of the call's result, run in a child so a hang is a TimeoutExpired, not a stuck test run."""
    try:
        r = subprocess.run(
            [sys.executable, "-c", _CHILD, str(home)], capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"_read_install_tier did not return within {TIMEOUT_SECONDS}s: it blocked on config.json")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    return r.stdout.strip().splitlines()[-1]


def test_c1_a_fifo_at_config_json_returns_none_instead_of_hanging(tmp_path):
    os.mkfifo(tmp_path / "config.json")
    assert _read_tier(tmp_path) == "None"


def test_c2_a_directory_at_config_json_returns_none(tmp_path):
    (tmp_path / "config.json").mkdir()
    assert _read_tier(tmp_path) == "None"


@pytest.mark.parametrize(
    "content, expected",
    [
        ('{"install_tier": "dev"}', "'dev'"),
        ('{"install_tier": "convenience"}', "'convenience'"),
        ('{"install_tier": 3}', "None"),
        ('{"other": "x"}', "None"),
        ('["install_tier"]', "None"),
        ("{not json", "None"),
        ("", "None"),
    ],
)
def test_c3_regular_files_behave_as_before(tmp_path, content, expected):
    (tmp_path / "config.json").write_text(content, encoding="utf-8")
    assert _read_tier(tmp_path) == expected


def test_c3_an_absent_config_json_returns_none(tmp_path):
    assert _read_tier(tmp_path) == "None"


# ---- C4: the whole start(), on a seat that supports the owner ---------------------------------------

from _owner_platform import requires_linux_owner  # noqa: E402

_START_CHILD = textwrap.dedent(
    """
    import os, sqlite3, sys
    from pathlib import Path
    sys.path.insert(0, {src!r})
    import engram_core, engramd_owner
    home = Path(sys.argv[1])
    os.environ["ENGRAM_HOME"] = str(home)
    conn = sqlite3.connect(str(home / "knowledge.db")); conn.row_factory = sqlite3.Row
    engram_core._run_db_one_time_setup(conn); conn.close()
    os.mkfifo(home / "config.json")   # the FIFO is there BEFORE start()
    owner = engramd_owner.EngramdOwner(home, request_deadline=2.0)
    owner.start(install_signal_handlers=False)
    try:
        print("STARTED")
    finally:
        owner.stop()
    """
).format(src=SRC)


@requires_linux_owner
def test_c4_start_with_a_fifo_at_config_json_returns_and_serves():
    tmp_path = Path(tempfile.mkdtemp(prefix="it3838-"))  # short: the socket path has a length limit
    try:
        _run_start(tmp_path)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _run_start(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    try:
        r = subprocess.run(
            [sys.executable, "-c", _START_CHILD, str(tmp_path)],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("EngramdOwner.start() did not return within 30s with a FIFO at config.json")
    assert r.returncode == 0 and "STARTED" in r.stdout, (r.returncode, r.stdout, r.stderr[-600:])
