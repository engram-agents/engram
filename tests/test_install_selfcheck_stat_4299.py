"""install_selfcheck tells "absent" from "could not be checked" (#4299).

``Path.is_file()`` / ``Path.exists()`` / ``Path.is_dir()`` raised PermissionError for a path under an
unreadable directory up to Python 3.12; on 3.14 they answer False, the same as for a missing path. A tool
that decides "missing" with them then reports an existing-but-unreadable file as "<path> does not exist",
with a remedy written for a missing file (for the store: "run bootstrap.py or restore the graph").

Contract (the spec's, stated before the fix):

* a path that is ABSENT is the same FAIL as before, byte for byte (message and remedy);
* a path that could not be CHECKED (any other OSError) is a FAIL whose detail names the error (strerror,
  errno name, path) and whose remedy says to check permissions; it never says "does not exist" and never
  suggests bootstrap, restore or re-install;
* both hold on every interpreter. The seat may run 3.12, so the 3.14 behaviour is emulated in the child
  process: ``Path.is_file/exists/is_dir`` are replaced by their ``os.path`` forms, which swallow every
  OSError (the measured 3.14 behaviour), then the tool is run with ``runpy`` as ``__main__``.
"""

import os
import subprocess
import sys

import pytest

from test_install_selfcheck import TOOL, Seat, _line_for  # noqa: F401  (the fixture home and line helpers)

RUNNER = '''\
import os, pathlib, runpy, sys
pathlib.Path.is_file = lambda self: os.path.isfile(self)
pathlib.Path.exists = lambda self: os.path.exists(self)
pathlib.Path.is_dir = lambda self: os.path.isdir(self)
tool = sys.argv[1]
sys.argv = [tool] + sys.argv[2:]
runpy.run_path(tool, run_name="__main__")
'''

SEMANTICS = ["native", "py314"]

needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="running as root: chmod 0 does not deny access")


def run_tool(seat, semantics, *args):
    if semantics == "native":
        return seat.run(*args)
    runner = seat.tmp / "py314-runner.py"
    runner.write_text(RUNNER)
    return subprocess.run(
        [sys.executable, str(runner), str(TOOL), *args], env=seat.env(),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)


class Unreadable:
    """chmod 0 a directory for the duration of the block; the mode is restored in a finally."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.mode = self.path.stat().st_mode & 0o7777
        self.path.chmod(0)
        return self

    def __exit__(self, *exc):
        self.path.chmod(self.mode)


def assert_uncheckable(line, path):
    assert line.startswith("FAIL "), line
    assert "does not exist" not in line, line
    for word in ("bootstrap", "restore", "re-run", "re-install", "reinstall", "create it"):
        assert word not in line, (word, line)
    assert str(path) in line, line
    assert "Permission denied" in line, line      # strerror
    assert "EACCES" in line, line                 # errno name
    assert "permissions" in line, line            # the remedy


# ── 1: the store ─────────────────────────────────────────────────────────────

@needs_non_root
@pytest.mark.parametrize("semantics", SEMANTICS)
def test_unreadable_parent_of_the_store_is_fail_naming_the_error(tmp_path, semantics):
    seat = Seat(tmp_path)
    with Unreadable(seat.engram):
        r = run_tool(seat, semantics)
    line = _line_for(r, "graph-opens")
    assert_uncheckable(line, seat.db)
    # the other two existence sites in the same directory: each must read "could not be checked" too
    assert_uncheckable(_line_for(r, "deployed-version"), seat.engram / ".deployed-version")
    assert_uncheckable(_line_for(r, "seed-ids-present"), seat.engram / "seed-manifest.json")
    assert r.returncode == 1


@needs_non_root
@pytest.mark.parametrize("semantics", SEMANTICS)
def test_unreadable_parent_of_the_store_pre_first_session(tmp_path, semantics):
    seat = Seat(tmp_path, phase="pre-first-session")
    with Unreadable(seat.engram):
        r = run_tool(seat, semantics, "--phase", "pre-first-session")
    assert_uncheckable(_line_for(r, "seed-graph"), seat.db)
    assert_uncheckable(_line_for(r, "first-run-pending"), seat.engram / "first-run-pending")


# ── 2: one tree check and one venv check ─────────────────────────────────────

@needs_non_root
@pytest.mark.parametrize("semantics", SEMANTICS)
def test_unreadable_plugin_dir_is_fail_naming_the_error(tmp_path, semantics):
    seat = Seat(tmp_path)
    with Unreadable(seat.plugin):
        r = run_tool(seat, semantics)
    assert_uncheckable(_line_for(r, "plugin-tree"), seat.plugin / "plugin.json")
    # the handshake's .mcp.json read is in the same directory (the engram home, where the marker is, stays
    # readable, so the handshake check is reached); it must not start the server either
    assert_uncheckable(_line_for(r, "mcp-handshake"), seat.plugin / ".mcp.json")
    assert not (seat.engram / "handshake-env-seen").exists()


@needs_non_root
@pytest.mark.parametrize("semantics", SEMANTICS)
def test_unreadable_venv_dir_is_fail_naming_the_error(tmp_path, semantics):
    seat = Seat(tmp_path)
    with Unreadable(seat.venv_py.parent):
        r = run_tool(seat, semantics)
    assert_uncheckable(_line_for(r, "python"), seat.venv_py)
    assert_uncheckable(_line_for(r, "venv-imports"), seat.venv_py)


@needs_non_root
@pytest.mark.parametrize("semantics", SEMANTICS)
def test_unreadable_claude_home_is_fail_naming_the_error(tmp_path, semantics):
    seat = Seat(tmp_path)
    with Unreadable(seat.claude_home):
        r = run_tool(seat, semantics)
    assert_uncheckable(_line_for(r, "claude-md-present"), seat.claude_home / "CLAUDE.md")


# ── 2b: the handshake's marker snapshot ──────────────────────────────────────

@needs_non_root
@pytest.mark.parametrize("semantics", SEMANTICS)
def test_unreadable_marker_is_fail_naming_the_error_and_starts_no_server(tmp_path, semantics):
    # chmod 0 on the marker FILE: the directory stays listable, so only the marker read fails. Its state is
    # unknown, so the check must not start the server (it exists to never disturb a live seat).
    seat = Seat(tmp_path)
    marker = seat.engram / "mcp-tools-ready.json"
    data = b'{"pid": 999999999}\n'
    marker.write_bytes(data)
    mode = marker.stat().st_mode & 0o7777
    marker.chmod(0)
    try:
        r = run_tool(seat, semantics)
    finally:
        marker.chmod(mode)
    assert_uncheckable(_line_for(r, "mcp-handshake"), marker)
    assert not (seat.engram / "handshake-env-seen").exists()   # the fake launcher writes it on every start
    assert marker.read_bytes() == data                          # and the marker was left as found


@pytest.mark.parametrize("semantics", SEMANTICS)
def test_marker_that_is_absent_by_a_non_enoent_errno_is_still_absent(tmp_path, semantics):
    # A marker that is a symlink to itself fails with ELOOP, one of the errnos that means "absent" (the same set
    # the existence predicates use). It must read as absent, not as "could not be checked": a handshake that
    # catches only FileNotFoundError would FAIL here.
    seat = Seat(tmp_path)
    marker = seat.engram / "mcp-tools-ready.json"
    marker.symlink_to(marker.name)
    r = run_tool(seat, semantics)
    assert _line_for(r, "mcp-handshake").startswith("PASS "), r.stdout
    assert (seat.engram / "handshake-env-seen").exists()       # the server was started


# ── 3: absent stays absent, byte for byte, under both semantics ──────────────

@pytest.mark.parametrize("semantics", SEMANTICS)
def test_missing_store_keeps_todays_message_and_remedy(tmp_path, semantics):
    seat = Seat(tmp_path)
    seat.db.unlink()
    r = run_tool(seat, semantics)
    line = _line_for(r, "graph-opens")
    assert line.startswith("FAIL graph-opens: %s does not exist — run bootstrap.py (fresh install) or restore the graph"
                           % seat.db), line


@pytest.mark.parametrize("semantics", SEMANTICS)
def test_missing_tree_venv_and_marker_files_keep_todays_wording(tmp_path, semantics):
    seat = Seat(tmp_path)
    (seat.plugin / "plugin.json").unlink()
    (seat.engram / ".deployed-version").unlink()
    (seat.engram / "seed-manifest.json").unlink()
    (seat.claude_home / "CLAUDE.md").unlink()
    seat.venv_py.unlink()
    r = run_tool(seat, semantics)
    assert _line_for(r, "plugin-tree") == (
        "FAIL plugin-tree: %s does not exist — re-run tools/install-local-marketplace.sh" % (seat.plugin / "plugin.json"))
    assert _line_for(r, "deployed-version") == (
        "FAIL deployed-version: %s does not exist — re-run tools/install-local-marketplace.sh"
        % (seat.engram / ".deployed-version"))
    assert _line_for(r, "seed-ids-present") == (
        "FAIL seed-ids-present: %s does not exist — run bootstrap.py, which writes it"
        % (seat.engram / "seed-manifest.json"))
    assert _line_for(r, "claude-md-present") == (
        "FAIL claude-md-present: %s does not exist — run bootstrap.py to render it from the template"
        % (seat.claude_home / "CLAUDE.md"))
    assert _line_for(r, "python").startswith("FAIL python: venv python not found at %s — create it:" % seat.venv_py)


@pytest.mark.parametrize("semantics", SEMANTICS)
def test_a_directory_where_a_file_is_expected_still_reads_as_absent(tmp_path, semantics):
    # An existing path that is not a regular file keeps today's verdict on every interpreter: "does not exist".
    seat = Seat(tmp_path)
    (seat.plugin / "plugin.json").unlink()
    (seat.plugin / "plugin.json").mkdir()
    r = run_tool(seat, semantics)
    assert _line_for(r, "plugin-tree") == (
        "FAIL plugin-tree: %s does not exist — re-run tools/install-local-marketplace.sh" % (seat.plugin / "plugin.json"))
