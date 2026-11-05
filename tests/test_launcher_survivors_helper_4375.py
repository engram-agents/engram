"""#4375 / PR 4386 review (Borges, REQUEST_CHANGES): the teardown that proves no launcher survives a test
must be able to FAIL on every platform it runs on.

The first version ran `ps eww -axo pid=,command=`. procps refuses to mix the BSD `eww` with the UNIX
`-axo` (rc 1, "must set personality to get -x option", empty stdout), and the code read only stdout, so on
Linux the list of survivors was always empty whatever survived. A failed listing read as an empty one: the
same blindness #4374's `_ps_listing_text` guards against.

`tests/_launcher_survivors.py` now holds the one implementation, both teardown sites use it, and these
cells pin that it (a) finds a real marked process, (b) refuses a ps that exits nonzero, (c) refuses a
listing that does not contain this process, (d) refuses an empty listing.
"""

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _launcher_survivors as ls  # noqa: E402


def _shim(tmp_path, body):
    d = tmp_path / "shimbin"
    d.mkdir()
    p = d / "ps"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(d) + os.pathsep + os.environ.get("PATH", "")


def test_finds_a_real_marked_launcher_like_process(tmp_path):
    marker = f"/tmp/marker-4375-{os.getpid()}-{time.monotonic_ns()}"
    p = subprocess.Popen(
        # two commands, so bash does not exec the sleep and drop the $0 that names the launcher
        ["bash", "-c", "sleep 30; :", "start-engram-daemon.sh"],
        env=dict(os.environ, ENGRAM_HOME=marker),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        time.sleep(0.3)
        assert str(p.pid) in ls.surviving_launchers(marker)
    finally:
        os.killpg(p.pid, 9)  # the whole group: bash and the sleep it started
        p.wait()


def test_a_home_nobody_uses_finds_nothing():
    assert ls.surviving_launchers(f"/tmp/no-such-home-4375-{time.monotonic_ns()}") == []


def test_a_ps_that_exits_nonzero_is_an_error_not_an_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _shim(tmp_path, "echo 'ps: error: must set personality' >&2; exit 1"))
    with pytest.raises(AssertionError, match="ps"):
        ls.surviving_launchers("/tmp/x")


def test_a_listing_without_this_process_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _shim(tmp_path, "echo '    1 /sbin/launchd'"))
    with pytest.raises(AssertionError, match="own pid|this process"):
        ls.surviving_launchers("/tmp/x")


def test_an_empty_successful_listing_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _shim(tmp_path, "exit 0"))
    with pytest.raises(AssertionError):
        ls.surviving_launchers("/tmp/x")


def test_a_nonzero_exit_with_a_plausible_listing_is_still_an_error(tmp_path, monkeypatch):
    """A ps that dies part-way and prints a prefix of the table that contains this process must not be
    read as a full listing (the nonzero-exit check is not redundant with the own-pid check)."""
    monkeypatch.setenv(
        "PATH",
        _shim(tmp_path, 'echo "    1 /sbin/launchd"; echo "$PPID python pytest"; exit 1'),
    )
    with pytest.raises(AssertionError, match="exited 1"):
        ls.surviving_launchers("/tmp/x")


def test_the_form_procps_refuses_is_not_the_form_used(tmp_path, monkeypatch):
    """Emulates procps on any host: the mixed BSD `eww` + UNIX `-axo` form exits 1 ('must set
    personality to get -x option'); the all-BSD form works. The helper must use the one that works and
    still find a marked launcher row."""
    marker = "/tmp/marker-4375-shim"
    body = (
        'case "$*" in *eww*-axo*) echo "error: must set personality to get -x option" >&2; exit 1;; esac\n'
        'echo "    1 /sbin/launchd"\n'
        'echo "$PPID python pytest"\n'
        f'echo "777 bash start-engram-daemon.sh ENGRAM_HOME={marker}"\n'
    )
    monkeypatch.setenv("PATH", _shim(tmp_path, body))
    assert ls.surviving_launchers(marker) == ["777"]
