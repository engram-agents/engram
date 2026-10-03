"""`_is_supervisor_process` -- the ONE identity guard behind R6/R7 (reap a stale
supervisor / single instance per uid) and `--check` (PR #3338 colleague round).

Contract: argv[0] must be a python interpreter, compared CASE-INSENSITIVELY (the
python.org macOS framework interpreter is `.../Python.app/Contents/MacOS/Python`,
capital P), AND a later argv token must be `monitor-supervisor.py` as a path
component. A non-python argv[0], or a python running a different script, is False.

Each test's docstring names the mutation that must turn it red.
"""

from __future__ import annotations

import re
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_monitor_supervisor_check import _load_module  # noqa: E402

_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "darwin_check" / "ps_snapshot.txt"
_SCRIPT = "/Users/seat/.engram/marketplace/plugins/engram/tools/monitor-supervisor.py"


def _framework_supervisor_args() -> str:
    """The real Mac supervisor row's args column (pid ppid stat args)."""
    for line in _SNAPSHOT.read_text().splitlines():
        m = re.match(r"\s*\d+\s+\d+\s+\S+\s+(\S*/Python\.app/Contents/MacOS/Python\s+\S*monitor-supervisor\.py.*)$", line)
        if m:
            return m.group(1)
    raise AssertionError("no framework-python supervisor row in ps_snapshot.txt")


def test_framework_python_argv_from_real_fixture_is_a_supervisor():
    """Real Mac fixture argv (argv[0] ends `/MacOS/Python`) -> True.
    Mutation: drop `.lower()` on the basename compare -> red (this test only)."""
    mod = _load_module()
    args = _framework_supervisor_args()
    assert args.split()[0].endswith("/MacOS/Python")  # the fixture really is capital-P
    assert mod._is_supervisor_process(args) is True


def test_lowercase_python3_argv_still_a_supervisor():
    """Control: the Linux shape stays True. Mutation: guard rewritten to match
    only `Python` -> red."""
    mod = _load_module()
    assert mod._is_supervisor_process(f"/usr/bin/python3 {_SCRIPT} --purpose collab-loop") is True


def test_non_python_argv0_running_the_script_path_is_not_a_supervisor():
    """A non-python argv[0] naming the script as a token -> False (pid-reuse
    protection). Mutation: drop the argv[0] check entirely (or compare with
    `in`/`.lower().find`) -> red."""
    mod = _load_module()
    assert mod._is_supervisor_process(f"/bin/cat {_SCRIPT}") is False


def test_python_running_a_different_script_is_not_a_supervisor():
    """Python (either case) running another script -> False. Mutation: drop the
    script-token check -> red."""
    mod = _load_module()
    assert mod._is_supervisor_process("/usr/bin/python3 /home/x/other-tool.py") is False
    assert mod._is_supervisor_process(
        "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python /x/other-tool.py"
    ) is False


def test_reap_accepts_framework_argv_and_kills_the_group(tmp_path, monkeypatch):
    """The reap path (R6/R7) shown to accept the framework argv: a live prior
    pid whose cmdline is the real Mac supervisor argv gets its group SIGKILLed.
    Mutation: drop `.lower()` -> the reap REFUSES (no killpg SIGKILL) -> red."""
    mod = _load_module()
    pidfile = tmp_path / "sup.pid"
    pidfile.write_text("424242\n")
    calls = []

    def fake_killpg(pgid, sig):
        calls.append((pgid, sig))

    monkeypatch.setattr(mod.os, "killpg", fake_killpg)
    monkeypatch.setattr(mod, "_pid_cmdline_status", lambda pid: ("ok", _framework_supervisor_args()))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    mod.reap_stale_supervisor(str(pidfile))
    assert (424242, signal.SIGKILL) in calls
