"""Contract tests for the AF_UNIX socket-path length limit (#3696, part of
#2652) -- Linux, and Darwin since #3666 (same cells, per-platform limit).

The contract (written before the implementation):

  K1  the owner checks the socket path's BYTE length in start(), right after
      _check_platform_support() and before the first filesystem mutation
      (engram_home.mkdir) -- an over-long path leaves neither engram_home
      nor run/ behind.
  K2  the refusal is a named exception, OwnerSocketPathTooLong, whose
      message carries the path's byte length, the limit and the remedy.
  K3  the limit is one named constant plus one helper in engramd_client, used
      by owner and client alike.
  K4  the client reports an over-long path as Unavailable with the SAME
      reason text, without ever opening a socket.
  K5  nothing changes on healthy paths: a path at exactly the longest
      bindable length starts, serves, and answers through the client.

Environment pinning: every test builds its own ENGRAM_HOME under a fresh
tempfile.mkdtemp() directory and passes it to EngramdOwner explicitly, so
the ambient ENGRAM_HOME / HOME are never read. The pad length is computed
from the actual tmp base length (TMPDIR-dependent); if the base is too long
for a non-negative pad the test skips with a reason rather than failing.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

# MEASURED on Linux 6.6 (WSL2), Python AF_UNIX bind on an absolute path in an
# existing directory, 2026-10-11 (#3696): a 107-byte path binds; a 108-byte
# path raises "OSError: AF_UNIX path too long". sun_path is 108 bytes
# including the NUL.
# MEASURED on macOS 26.6.2 / Python 3.12.10, same probe (#3666): a 103-byte
# path binds; a 104-byte path raises "AF_UNIX path too long" (sun_path is 104
# per the header). Literals, not ec's constants, so the constants are checked
# against a measurement; test_kernel_boundary_matches_constants re-measures
# both on the running seat so these cannot drift from reality.
if sys.platform == "darwin":
    LONGEST_BINDABLE_BYTES = 103
    SHORTEST_FAILING_BYTES = 104
else:
    LONGEST_BINDABLE_BYTES = 107
    SHORTEST_FAILING_BYTES = 108

REQUEST_DEADLINE = 0.6
_TAIL = "/run/engramd.sock"  # appended to ENGRAM_HOME: 17 bytes


def _blen(p) -> int:
    return len(os.fsencode(p))


@pytest.fixture
def tmp_base():
    d = tempfile.mkdtemp(prefix="edo-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _home_for_socket_bytes(base: Path, total: int, *, multibyte: int = 0) -> Path:
    """An ENGRAM_HOME (not created) under `base` whose socket path
    (<home>/run/engramd.sock) is exactly `total` BYTES. With multibyte=k the
    final component starts with k copies of U+00E9 (2 bytes each), so the
    path's character count is k below its byte count."""
    fixed = _blen(base) + 1 + len(_TAIL)  # base + "/" + tail
    comp_bytes = total - fixed
    if comp_bytes < 2 * multibyte + 1:
        pytest.skip(f"tmp base {base} too long for a {total}-byte socket path")
    comp = "é" * multibyte + "h" * (comp_bytes - 2 * multibyte)
    home = base / comp
    assert _blen(home / "run" / "engramd.sock") == total
    return home


def _fs_accepts_multibyte(base: Path) -> bool:
    probe = base / "éprobe"
    try:
        probe.mkdir()
    except OSError:
        return False
    probe.rmdir()
    return True


def _new_owner(home: Path) -> "eo.EngramdOwner":
    return eo.EngramdOwner(home, request_deadline=REQUEST_DEADLINE)


# -- T1 (a): over-long -> named refusal, nothing created -----------------------


@requires_linux_owner
def test_overlong_path_refused_by_name_and_nothing_created(tmp_base):
    home = _home_for_socket_bytes(tmp_base, SHORTEST_FAILING_BYTES)
    owner = _new_owner(home)
    with pytest.raises(eo.OwnerSocketPathTooLong) as ei:
        owner.start(install_signal_handlers=False)
    msg = str(ei.value)
    assert str(SHORTEST_FAILING_BYTES) in msg
    assert str(LONGEST_BINDABLE_BYTES) in msg
    # the REMEDY, not merely the word: the layout sentence also contains "ENGRAM_HOME"
    assert "Shorten ENGRAM_HOME" in msg
    assert not home.exists(), "engram_home was created before the refusal"
    assert not (home / "run").exists(), "run/ was created before the refusal"
    assert isinstance(ei.value, RuntimeError)
    # the tail figure in the message is the real length of "/run/engramd.sock"
    assert f"adds {len(_TAIL)} bytes" in msg


@requires_linux_owner
@pytest.mark.parametrize("run_exists", [False, True], ids=["home-exists", "home-and-run-exist"])
def test_overlong_path_with_an_existing_home_is_refused_untouched(tmp_base, run_exists):
    # Every refusal test above uses an ENGRAM_HOME that does not exist yet. A real seat's
    # home always exists, and a check that only ran for a missing home would pass those
    # tests while start() went on to mkdir/harden and die in bind() with the raw traceback.
    home = _home_for_socket_bytes(tmp_base, SHORTEST_FAILING_BYTES)
    home.mkdir()
    if run_exists:
        (home / "run").mkdir()
        os.chmod(home / "run", 0o755)  # _harden_run_dir() would force 0o700
    with pytest.raises(eo.OwnerSocketPathTooLong):
        _new_owner(home).start(install_signal_handlers=False)
    assert (home / "run").exists() is run_exists, "run/ was created or removed by a refused start"
    if run_exists:
        assert (os.stat(home / "run").st_mode & 0o777) == 0o755, "run/ was hardened by a refused start"
    assert not (home / "run" / "engramd.sock").exists()
    assert sorted(p.name for p in home.iterdir()) == (["run"] if run_exists else []), "refused start left files"


# -- T2 (b): exactly the longest bindable path starts and serves ---------------


@requires_linux_owner
def test_longest_bindable_path_starts_and_answers(tmp_base):
    home = _home_for_socket_bytes(tmp_base, LONGEST_BINDABLE_BYTES)
    owner = _new_owner(home)
    owner.start(install_signal_handlers=False)
    try:
        assert _blen(owner.socket_path) == LONGEST_BINDABLE_BYTES
        result = ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path)
        assert isinstance(result, ec.Ok)  # T7: the client path, same healthy pin
    finally:
        owner.stop()


# -- T4 (d): bytes, not characters ----------------------------------------------


@requires_linux_owner
def test_byte_length_not_character_length_decides_refusal(tmp_base):
    if not _fs_accepts_multibyte(tmp_base):
        pytest.skip("filesystem rejects multi-byte file names")
    home = _home_for_socket_bytes(tmp_base, SHORTEST_FAILING_BYTES, multibyte=3)
    sp = home / "run" / "engramd.sock"
    assert len(str(sp)) < SHORTEST_FAILING_BYTES <= _blen(sp)
    with pytest.raises(eo.OwnerSocketPathTooLong):
        _new_owner(home).start(install_signal_handlers=False)
    assert not home.exists()


@requires_linux_owner
def test_multibyte_path_at_the_limit_in_bytes_starts(tmp_base):
    if not _fs_accepts_multibyte(tmp_base):
        pytest.skip("filesystem rejects multi-byte file names")
    home = _home_for_socket_bytes(tmp_base, LONGEST_BINDABLE_BYTES, multibyte=3)
    owner = _new_owner(home)
    owner.start(install_signal_handlers=False)
    try:
        assert len(str(owner.socket_path)) < LONGEST_BINDABLE_BYTES
        assert isinstance(
            ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path), ec.Ok
        )
    finally:
        owner.stop()


# -- T5: the constants match the kernel on this seat ----------------------------


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="sun_path limit measured only for Linux and Darwin",
)
def test_kernel_boundary_matches_constants(tmp_base):
    def try_bind(total: int):
        name = "k" * (total - _blen(tmp_base) - 1)
        p = tmp_base / name
        assert _blen(p) == total
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(p))
        finally:
            s.close()
            if p.exists():
                p.unlink()

    if _blen(tmp_base) + 2 > LONGEST_BINDABLE_BYTES:
        pytest.skip("tmp base too long to build the probe paths")
    try_bind(LONGEST_BINDABLE_BYTES)  # must not raise
    with pytest.raises(OSError, match="path too long"):
        try_bind(SHORTEST_FAILING_BYTES)
    assert ec.SOCKET_PATH_MAX_BYTES == LONGEST_BINDABLE_BYTES


# -- T6: the client reports it too ----------------------------------------------


def test_client_overlong_path_is_unavailable_with_same_reason_and_no_socket(
    tmp_base, monkeypatch
):
    sp = _home_for_socket_bytes(tmp_base, SHORTEST_FAILING_BYTES) / "run" / "engramd.sock"

    def _no_socket(*a, **k):
        raise AssertionError("client opened a socket for an over-long path")

    monkeypatch.setattr(ec.socket, "socket", _no_socket)
    res = ec.read("ping", {}, timeout=1.0, socket_path=sp)
    assert isinstance(res, ec.Unavailable)
    assert str(SHORTEST_FAILING_BYTES) in res.detail
    assert str(LONGEST_BINDABLE_BYTES) in res.detail
    assert "ENGRAM_HOME" in res.detail
    assert res.detail == ec.socket_path_too_long_reason(sp)
    res_w = ec.write("noop", {}, timeout=1.0, socket_path=sp)
    assert isinstance(res_w, ec.Unavailable)
    assert res_w.detail == res.detail


@requires_linux_owner
def test_owner_and_client_share_the_same_reason_text(tmp_base):
    home = _home_for_socket_bytes(tmp_base, SHORTEST_FAILING_BYTES)
    sp = home / "run" / "engramd.sock"
    with pytest.raises(eo.OwnerSocketPathTooLong) as ei:
        _new_owner(home).start(install_signal_handlers=False)
    assert str(ei.value) == ec.socket_path_too_long_reason(sp)


def test_helper_returns_none_when_the_path_fits(tmp_base):
    sp = _home_for_socket_bytes(tmp_base, LONGEST_BINDABLE_BYTES) / "run" / "engramd.sock"
    assert ec.socket_path_too_long_reason(sp) is None
    assert ec.socket_path_too_long_reason(Path("/tmp/x/run/engramd.sock")) is None


# -- main(): a clean non-zero exit, not a traceback -----------------------------


@requires_linux_owner
def test_main_exits_nonzero_with_message_on_overlong_path(tmp_base, monkeypatch, capsys):
    home = _home_for_socket_bytes(tmp_base, SHORTEST_FAILING_BYTES)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert eo.main() == 1
    err = capsys.readouterr().err
    assert "ENGRAM_HOME" in err and str(SHORTEST_FAILING_BYTES) in err
    assert not home.exists()
