"""Contract tests for engram-alpha#3714 -- a Darwin arm for engram_pidfile's
pid-identity check (libproc start time, KERN_PROCARGS2 argv).

Written BEFORE the Darwin arm exists (contract-first). Every cell uses real
processes on a real Mac and SKIPS elsewhere; the primitive is never
simulated, except where a cell says it patches one raw layer to force an
outcome the OS will not produce on demand (ESRCH race, size mismatch,
cross-uid EINVAL).

Names the spec leaves to the implementer and these cells pin:
`_darwin_proc_pidinfo(pid) -> (nbytes, errno, buf)`,
`_darwin_procargs2(pid) -> (bytes | None, errno)`,
`_read_process_start_time_darwin(pid) -> float | None` (raises
ProcessLookupError on ESRCH), `_read_process_cmdline_darwin(pid) -> str | None`.
"""

from __future__ import annotations

import errno
import os
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_pidfile as ep  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Darwin pid-identity arm (libproc / KERN_PROCARGS2); real-Mac cells only",
)

MARKER = "engramd_owner_3714"


@pytest.fixture
def pidfile(tmp_path):
    return tmp_path / "engramd.pid"


@pytest.fixture
def children():
    procs = []

    def spawn(argv):
        p = subprocess.Popen(argv)
        procs.append(p)
        return p

    try:
        yield spawn
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()


def _write_pidfile(path, pid, age_s=0.0):
    path.write_text(str(pid))
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))


def _wait_readable(pid):
    # A just-spawned child can be momentarily unreadable; wait for it.
    for _ in range(100):
        try:
            if ep._read_process_cmdline_darwin(pid) is not None:
                return
        except ProcessLookupError:
            pass
        time.sleep(0.01)


def test_a_reused_pid_reads_dead(pidfile, children):
    child = children(["/bin/sleep", "60"])
    _write_pidfile(pidfile, child.pid, age_s=600)
    assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is False


def test_b_live_writer_reads_alive(pidfile, children):
    child = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    _write_pidfile(pidfile, child.pid)  # written AFTER the child started
    assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is True


def test_c_dead_pid_reads_dead(pidfile):
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    _write_pidfile(pidfile, p.pid)
    assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is False


def test_d_start_time_matches_ps_lstart(children):
    child = children(["/bin/sleep", "60"])
    lstart = subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(child.pid)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    ps_epoch = datetime.strptime(lstart, "%a %b %d %H:%M:%S %Y").timestamp()
    st = ep._read_process_start_time_darwin(child.pid)
    assert st is not None
    assert abs(st - ps_epoch) <= 1.0, (st, ps_epoch)


def test_e_esrch_after_successful_kill_reads_dead(pidfile, children, monkeypatch):
    child = children(["/bin/sleep", "60"])
    _write_pidfile(pidfile, child.pid, age_s=600)
    os.kill(child.pid, 0)  # a real kill(pid, 0) succeeds ...
    # ... then the process "dies" before the start-time read: libproc reports
    # 0 bytes with ESRCH.
    monkeypatch.setattr(
        ep, "_darwin_proc_pidinfo", lambda pid: (0, errno.ESRCH, b""), raising=False
    )
    assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is False


def test_f_cross_uid_argv_einval_reads_alive(pidfile, children, monkeypatch):
    # Start time (real) says "started after the pidfile"; argv is unreadable
    # as it is cross-uid (EINVAL, patched at the raw sysctl layer because
    # os.kill(1, 0) would fail with EPERM first and never reach the arm).
    child = children(["/bin/sleep", "60"])
    _write_pidfile(pidfile, child.pid, age_s=600)
    monkeypatch.setattr(
        ep, "_darwin_procargs2", lambda pid: (None, errno.EINVAL), raising=False
    )
    assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is True


def test_f2_real_pid1_argv_is_unreadable():
    # The real EINVAL: launchd (root) is unreadable to a normal user.
    if os.geteuid() == 0:
        pytest.skip("running as root: pid 1 argv is readable")
    assert ep._read_process_cmdline_darwin(1) is None


def test_g_size_mismatch_is_conservative_alive_never_parsed(pidfile, children, monkeypatch):
    child = children(["/bin/sleep", "60"])
    _write_pidfile(pidfile, child.pid, age_s=600)
    # A buffer that WOULD parse to a reuse-shaped start time (pid matches,
    # started now) if the size check were skipped.
    buf = bytearray(136)
    struct.pack_into("I", buf, 12, child.pid)
    struct.pack_into("QQ", buf, 120, int(time.time()), 0)
    monkeypatch.setattr(
        ep, "_darwin_proc_pidinfo", lambda pid: (96, 0, bytes(buf)), raising=False
    )
    assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is True


def test_g2_pid_mismatch_is_not_parsed(children, monkeypatch):
    child = children(["/bin/sleep", "60"])
    buf = bytearray(136)
    struct.pack_into("I", buf, 12, child.pid + 1)
    struct.pack_into("QQ", buf, 120, int(time.time()), 0)
    monkeypatch.setattr(
        ep, "_darwin_proc_pidinfo", lambda pid: (136, 0, bytes(buf)), raising=False
    )
    assert ep._read_process_start_time_darwin(child.pid) is None


def test_h_marker_in_argv_reads_alive(pidfile, children, monkeypatch):
    child = children([sys.executable, "-c", "import time; time.sleep(60)", MARKER])
    _write_pidfile(pidfile, child.pid, age_s=600)  # reuse-shaped start time
    _wait_readable(child.pid)
    # Spy (wrap, not replace): a True here must come from the marker being READ,
    # not from an unreadable cmdline falling back to the conservative True.
    real = ep._read_process_cmdline_darwin
    seen = []

    def spy(pid):
        out = real(pid)
        seen.append((pid, out))
        return out

    monkeypatch.setattr(ep, "_read_process_cmdline_darwin", spy)
    assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is True
    assert any(pid == child.pid and out is not None and MARKER in out for pid, out in seen), seen


def test_argv_reader_returns_real_argv(children):
    child = children([sys.executable, "-c", "import time; time.sleep(60)", MARKER])
    _wait_readable(child.pid)
    cmdline = ep._read_process_cmdline_darwin(child.pid)
    assert cmdline is not None and cmdline.endswith("time.sleep(60) " + MARKER)


def test_empty_argv0_is_unreadable_not_parsed():
    # Round 3: an empty argv[0] is indistinguishable from layout drift (wider
    # padding puts a NUL at the computed start), so it reads as unreadable.
    p = subprocess.Popen(["", "30"], executable="/bin/sleep")
    try:
        time.sleep(0.2)  # cannot _wait_readable: None is the expected answer
        assert ep._read_process_cmdline_darwin(p.pid) is None
    finally:
        p.kill()
        p.wait()


def test_empty_entry_in_the_middle_still_parses_exactly():
    p = subprocess.Popen(["sleepy", "", "30"], executable="/bin/sleep")
    try:
        _wait_readable(p.pid)
        assert ep._read_process_cmdline_darwin(p.pid) == "sleepy  30"
    finally:
        p.kill()
        p.wait()


def test_reuse_shaped_child_with_empty_argv0_reads_alive(pidfile):
    p = subprocess.Popen(["", "60"], executable="/bin/sleep")
    try:
        time.sleep(0.2)
        _write_pidfile(pidfile, p.pid, age_s=600)  # reuse-shaped start time
        assert ep.recorded_pid_is_alive(pidfile, program_marker=MARKER) is True
    finally:
        p.kill()
        p.wait()


@pytest.mark.parametrize("extra", range(8))
@pytest.mark.parametrize("argv0", ["x", "", "abcdefghij"])
def test_argv_exact_across_exec_path_alignments(tmp_path, argv0, extra):
    # The padding between the exec path and argv[0] is computed from the
    # buffer (next multiple of 8 in raw[4:]); every residue mod 8 must give
    # the exact argv back.
    link = tmp_path / ("s" * (20 + extra))
    link.symlink_to("/bin/sleep")
    p = subprocess.Popen([argv0, "30"], executable=str(link))
    try:
        if argv0 == "":
            time.sleep(0.2)
            assert ep._read_process_cmdline_darwin(p.pid) is None
        else:
            _wait_readable(p.pid)
            assert ep._read_process_cmdline_darwin(p.pid) == f"{argv0} 30"
    finally:
        p.kill()
        p.wait()


def _procargs(path=b"/bin/x", pad=None, args=(b"a", b"b"), argc=None, env=(b"E=1",)):
    if pad is None:
        pad = b"\0" * ((-(len(path) + 1)) % 8)
    argc = len(args) if argc is None else argc
    body = path + b"\0" + pad + b"".join(a + b"\0" for a in args) + b"".join(e + b"\0" for e in env)
    return struct.pack("i", argc) + body


def test_inconsistent_padding_is_unreadable(monkeypatch):
    # A non-NUL byte inside the predicted padding: layout not as measured.
    raw = _procargs(pad=b"Q")
    monkeypatch.setattr(ep, "_darwin_procargs2", lambda pid: (raw, 0))
    assert ep._read_process_cmdline_darwin(123) is None


def test_wider_padding_than_measured_is_unreadable(monkeypatch):
    # 16-byte-style padding: 8 extra NULs before a non-empty argv[0]. The
    # padding region is still all NUL, so only the empty-argv[0] rule catches it.
    base = b"\0" * ((-(len(b"/bin/x") + 1)) % 8)
    raw = _procargs(pad=base + b"\0" * 8)
    monkeypatch.setattr(ep, "_darwin_procargs2", lambda pid: (raw, 0))
    assert ep._read_process_cmdline_darwin(123) is None


def test_negative_argc_is_unreadable(monkeypatch):
    raw = _procargs(argc=-1)
    monkeypatch.setattr(ep, "_darwin_procargs2", lambda pid: (raw, 0))
    assert ep._read_process_cmdline_darwin(123) is None


def test_short_argv_is_unreadable(monkeypatch):
    raw = _procargs(args=(b"a",), argc=3, env=())
    monkeypatch.setattr(ep, "_darwin_procargs2", lambda pid: (raw, 0))
    assert ep._read_process_cmdline_darwin(123) is None


def test_wellformed_synthetic_buffer_parses(monkeypatch):
    raw = _procargs()
    monkeypatch.setattr(ep, "_darwin_procargs2", lambda pid: (raw, 0))
    assert ep._read_process_cmdline_darwin(123) == "a b"


@pytest.fixture
def fresh_warning_state(monkeypatch):
    monkeypatch.setattr(ep, "_size_warned", False)


def test_eperm_zero_bytes_does_not_warn_or_burn_the_slot(caplog, monkeypatch, fresh_warning_state):
    monkeypatch.setattr(ep, "_darwin_proc_pidinfo", lambda pid: (0, errno.EPERM, b""))
    with caplog.at_level("WARNING", logger=ep.__name__):
        assert ep._read_process_start_time_darwin(1) is None
    assert not caplog.records
    assert ep._size_warned is False
    # ... and a real size mismatch afterwards still warns, exactly once.
    monkeypatch.setattr(ep, "_darwin_proc_pidinfo", lambda pid: (100, 0, b"\0" * 100))
    with caplog.at_level("WARNING", logger=ep.__name__):
        assert ep._read_process_start_time_darwin(1) is None
        assert ep._read_process_start_time_darwin(1) is None
    assert len(caplog.records) == 1
    assert ep._size_warned is True


def test_ctypes_signatures_are_declared():
    import ctypes

    libproc, libc = ep._darwin_libs()
    assert libproc.proc_pidinfo.restype is ctypes.c_int
    assert list(libproc.proc_pidinfo.argtypes) == [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    ]
    assert libc.sysctl.restype is ctypes.c_int
    assert list(libc.sysctl.argtypes) == [
        ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
    ]
