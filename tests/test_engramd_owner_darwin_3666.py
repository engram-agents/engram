"""Darwin arm of the engramd owner's permission layer -- issue #3666.

Contract (docs/2652-security-slice-design.md §2.1, §2.2, §2.5; build spec v1.1
on #3666). Every cell runs on a REAL Mac; none simulates Darwin by patching
`platform.system` alone (#3375). The whole file skips off Darwin, so Linux CI
is unaffected -- the Linux arm keeps its own cells in test_engramd_owner.py.

Cells (numbering follows the build spec):

  1. `run/` strip goes through the fd, with a swap control (A stripped, B not);
     plus: it is `/bin/chmod` by absolute path (a PATH-poisoned `chmod` is
     never run), and a non-zero `chmod -N` exit refuses.
  2. The ACL read-back refuses a leftover entry (mutation-shaped, see docstring).
  3. `LOCAL_PEERCRED`: short buffer, wrong `cr_version`, wrong level, uid 0,
     uid mismatch.
  4. Socket-path length: 103 starts, 104 refuses with NEITHER directory made.
  5. The refusal and the marker agree when each Darwin prerequisite is missing.
  6. TCC-protected `ENGRAM_HOME` refuses; outside the list it starts.
  7. A full listen backlog reads as `Unavailable` with a reason.
  8. End to end: the owner starts, a client `ping` succeeds, stop cleans up.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import _owner_platform as op  # noqa: E402  (imported at COLLECTION: it evaluates the predicate once, unpatched)
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
from _owner_store import seed_current_store  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Darwin arm of the owner permission layer (#3666): needs a real Mac",
)

REQUEST_DEADLINE = 0.6
_CHMOD = "/bin/chmod"  # literal on purpose: these cells do not trust the module's own constant


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def home():
    """A short-path temp ENGRAM_HOME (AF_UNIX paths are capped at 103 bytes here)."""
    d = tempfile.mkdtemp(prefix="d66-")
    seed_current_store(d)  # #3753: the owner refuses a home with no current-schema store
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _owner(h: Path) -> "eo.EngramdOwner":
    return eo.EngramdOwner(h, request_deadline=REQUEST_DEADLINE)


def _add_entry(path: Path) -> None:
    subprocess.run([_CHMOD, "+a", "everyone allow read,write", str(path)], check=True)


def _acl_lines(path: Path) -> list[str]:
    """ACL entries on `path`, by PATH, via /bin/ls -led -- independent of the
    production read-back (which goes through an fd)."""
    out = subprocess.run(
        ["/bin/ls", "-led", str(path)], capture_output=True, text=True, check=True
    ).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()[:1].isdigit() and ": " in ln]


def _seed_run_dir_with_entry(owner: "eo.EngramdOwner") -> Path:
    run = owner.socket_path.parent
    run.mkdir(parents=True)
    os.chmod(run, 0o700)
    _add_entry(run)
    assert _acl_lines(run), "positive control: the fixture run/ must carry an ACL entry"
    return run


def _is_strip_call(argv) -> bool:
    return isinstance(argv, (list, tuple)) and len(argv) >= 3 and argv[1] == "-N"


# ---------------------------------------------------------------------------
# Cell 1 -- the strip goes through the fd
# ---------------------------------------------------------------------------


def test_strip_acts_on_the_fd_target_not_the_path(home, monkeypatch):
    """Swap control (design doc §2.5): open run/ (A), rename it away, create a
    new run/ (B) with its own entry, then strip. A must be clean, B untouched.
    The swap happens INSIDE the chmod call, after the fd is open, so a
    path-based strip or a path-based read-back would see B and the cell fails.
    `_harden_run_dir()` finishing without a refusal also shows the read-back
    read A (clean) through the fd, not B (which carries an entry)."""
    owner = _owner(home)
    run = _seed_run_dir_with_entry(owner)
    moved = home / "run-A"
    real_run = subprocess.run
    swapped = []

    def swapping_run(argv, *a, **kw):
        if _is_strip_call(argv):
            os.rename(run, moved)
            os.mkdir(run, 0o700)
            _add_entry(run)
            swapped.append(True)
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", swapping_run)
    owner._harden_run_dir()

    assert swapped, "the swap fixture never fired: the strip did not call chmod -N"
    assert _acl_lines(moved) == [], "A (the fd's target) must be stripped"
    assert _acl_lines(run), "B (what the path names now) must be untouched"


def test_strip_uses_bin_chmod_by_absolute_path_never_path(home, monkeypatch):
    """A `chmod` earlier on PATH (a gnubin-first PATH, or worse) must never be
    run: the strip is `/bin/chmod` by absolute path. The fake exits 1 and drops
    a marker file; if the owner resolved `chmod` through PATH it would refuse
    (exit 1) and leave the marker."""
    fake_bin = home / "fakebin"
    fake_bin.mkdir()
    marker = home / "fake-chmod-ran"
    fake = fake_bin / "chmod"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    owner = _owner(home / "h")
    run = _seed_run_dir_with_entry(owner)
    owner._harden_run_dir()

    assert not marker.exists(), "a PATH `chmod` was executed"
    assert _acl_lines(run) == []


def test_nonzero_chmod_exit_refuses_like_the_linux_setfacl_failure(home, monkeypatch):
    owner = _owner(home)
    _seed_run_dir_with_entry(owner)
    real_run = subprocess.run

    def failing_run(argv, *a, **kw):
        if _is_strip_call(argv):
            return subprocess.CompletedProcess(argv, 1, "", "chmod: boom")
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(eo.OwnerPermissionUnsupported) as exc:
        owner._harden_run_dir()
    assert "chmod -N" in str(exc.value) and "exit 1" in str(exc.value)


# ---------------------------------------------------------------------------
# Cell 2 -- the read-back refuses a leftover entry
# ---------------------------------------------------------------------------


def test_readback_refuses_when_the_strip_leaves_an_entry(home, monkeypatch):
    """MUTATION-SHAPED BY NECESSITY. A real `chmod -N` never leaves an entry,
    so there is no honest way to reach this branch with the real tool. The
    fixture replaces the strip with a no-op that reports success (exit 0) on a
    run/ that carries an entry -- exactly the silent gap the read-back exists
    to close. Passing means the read-back caught it; it is the nearest thing
    to a mutation test of `drop the read-back`."""
    owner = _owner(home)
    run = _seed_run_dir_with_entry(owner)
    real_run = subprocess.run

    def noop_strip(argv, *a, **kw):
        if _is_strip_call(argv):
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", noop_strip)
    with pytest.raises(eo.OwnerAclNotStripped) as exc:
        owner._harden_run_dir()
    msg = str(exc.value)
    assert "read-back" in msg
    assert "read,write" in msg, f"the refusal must list the leftover entry: {msg}"
    assert _acl_lines(run), "control: the no-op strip really left the entry in place"


def _add_inheritable_entry(path: Path) -> None:
    subprocess.run(
        [_CHMOD, "+a", "everyone allow list,add_file,search,file_inherit,directory_inherit", str(path)],
        check=True,
    )


def test_run_dir_is_0700_with_no_acl_under_an_inheriting_parent(home):
    """Zephyr's #3703 question, answered with the real tool: a run/ created under
    a parent with an INHERITABLE entry starts with that entry (control), and after
    start() it reads mode 0700 with no ACL. The parent keeps its own entry."""
    _add_inheritable_entry(home)
    probe = home / "probe"
    probe.mkdir()
    assert _acl_lines(probe), "control: a child of this parent must inherit the entry"
    probe.rmdir()

    owner = _owner(home)
    owner.start(install_signal_handlers=False)
    try:
        run = owner.socket_path.parent
        assert stat_mode(run) == 0o700
        assert _acl_lines(run) == []
        assert _acl_lines(home), "control: the parent's own entry is untouched"
    finally:
        owner.stop()


def test_strip_refuses_if_the_mode_is_widened(home, monkeypatch):
    """The mode is read back through the fd as well as the ACL. A strip that
    exits 0 but leaves run/ wider than 0700 refuses. (The real `chmod -N` leaves
    mode bits alone -- measured; this fake models the regression.)"""
    owner = _owner(home)
    _seed_run_dir_with_entry(owner)
    real_run = subprocess.run

    def widening_strip(argv, *a, **kw):
        if _is_strip_call(argv):
            proc = real_run(argv, *a, **kw)
            os.fchmod(int(argv[2].rsplit("/", 1)[1]), 0o755)
            return proc
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", widening_strip)
    with pytest.raises(eo.OwnerAclNotStripped) as exc:
        owner._harden_run_dir()
    assert "0o755" in str(exc.value) and "0o700" in str(exc.value)


class _FakeFn:
    """Stands in for a ctypes foreign function: settable restype/argtypes."""

    def __init__(self, impl):
        self.impl, self.restype, self.argtypes = impl, None, None

    def __call__(self, *a):
        return self.impl(*a)


class _FakeLibc:
    def __init__(self, get_fd, to_text):
        self.freed = []
        self.acl_get_fd_np = _FakeFn(get_fd)
        self.acl_to_text = _FakeFn(to_text)
        self.acl_free = _FakeFn(lambda p: self.freed.append(p) or 0)


def _fail_with(err):
    def fn(*_a):
        ctypes.set_errno(err)
        return None
    return fn


def test_readback_refuses_when_libc_cannot_be_loaded(monkeypatch):
    def no_libc(*_a, **_k):
        raise OSError("dlopen failed")

    monkeypatch.setattr(ctypes, "CDLL", no_libc)
    with pytest.raises(eo.OwnerAclNotStripped) as exc:
        eo._darwin_acl_entries_via_fd(0)
    assert "unavailable" in str(exc.value)


def test_readback_refuses_on_an_acl_get_errno_other_than_enoent(monkeypatch):
    """NULL + ENOENT is the documented "no ACL"; NULL + anything else is a
    read-back that did not happen, never a clean one."""
    lib = _FakeLibc(get_fd=_fail_with(errno.EPERM), to_text=lambda *a: None)
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: lib)
    with pytest.raises(eo.OwnerAclNotStripped) as exc:
        eo._darwin_acl_entries_via_fd(0)
    assert f"errno {errno.EPERM}" in str(exc.value)

    clean = _FakeLibc(get_fd=_fail_with(errno.ENOENT), to_text=lambda *a: None)
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: clean)
    assert eo._darwin_acl_entries_via_fd(0) == [], "control: ENOENT still reads as no ACL"


def test_readback_refuses_when_the_acl_cannot_be_rendered_and_frees_it(monkeypatch):
    fake_acl = 0x1234
    lib = _FakeLibc(get_fd=lambda *a: fake_acl, to_text=_fail_with(errno.ENOMEM))
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: lib)
    with pytest.raises(eo.OwnerAclNotStripped) as exc:
        eo._darwin_acl_entries_via_fd(0)
    assert "could not render" in str(exc.value)
    assert lib.freed == [fake_acl], "the acl_t is freed exactly once on the failure path"


def test_a_hung_strip_refuses_with_a_named_reason(home, monkeypatch):
    owner = _owner(home)
    _seed_run_dir_with_entry(owner)
    real_run = subprocess.run

    def hung_strip(argv, *a, **kw):
        if _is_strip_call(argv):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", hung_strip)
    with pytest.raises(eo.OwnerPermissionUnsupported) as exc:
        owner._harden_run_dir()
    assert "did not finish" in str(exc.value)


# ---------------------------------------------------------------------------
# Cell 3 -- LOCAL_PEERCRED
# ---------------------------------------------------------------------------


def _xucred(*, version: int = 0, uid: int | None = None, ngroups: int = 1, gid: int = 20) -> bytes:
    uid = os.getuid() if uid is None else uid
    return struct.pack("@IIh16I", version, uid, ngroups, gid, *([0] * 15))


class _FakeConn:
    """Answers `getsockopt` like the kernel does: the real answer only for the
    exact (level, option, buflen) of a correct call; 4 zero bytes and no error
    for the wrong LEVEL (measured, design doc P3); records every call."""

    def __init__(self, cred: bytes | None = None, *, raw: bytes | None = None):
        self._cred = cred
        self._raw = raw
        self.calls: list[tuple] = []

    def getsockopt(self, level, optname, buflen):
        self.calls.append((level, optname, buflen))
        if self._raw is not None:
            return self._raw
        if (level, optname, buflen) == (0, socket.LOCAL_PEERCRED, 76):
            return self._cred
        return b"\x00\x00\x00\x00"


def test_xucred_layout_is_76_bytes_native_aligned():
    assert struct.calcsize("@IIh16I") == 76
    assert struct.calcsize("=IIh16I") == 74  # the trap §2.2 names


def test_peercred_correct_call_and_same_uid_accepts(home):
    conn = _FakeConn(_xucred())
    assert _owner(home)._check_peer_credentials(conn) is None
    assert conn.calls == [(0, socket.LOCAL_PEERCRED, 76)], "level 0 (SOL_LOCAL), option, 76 bytes"


def test_peercred_short_buffer_refuses_rather_than_reading_uid_0(home):
    """Measured: getsockopt(0, LOCAL_PEERCRED, 8) returns 8 bytes, no error.
    The parse must RAISE (unpack_from), never decode a slice to uid 0."""
    for raw in (b"\x00" * 4, b"\x00" * 8, _xucred()[:75]):
        with pytest.raises(struct.error):
            _owner(home)._check_peer_credentials(_FakeConn(raw=raw))


def test_peercred_overlong_buffer_refuses(home):
    with pytest.raises(eo.OwnerPeerCredentialsMalformed):
        _owner(home)._check_peer_credentials(_FakeConn(raw=_xucred() + b"\x00"))


def test_peercred_wrong_cr_version_refuses(home):
    with pytest.raises(eo.OwnerPeerCredentialsMalformed) as exc:
        _owner(home)._check_peer_credentials(_FakeConn(_xucred(version=1)))
    assert "cr_version" in str(exc.value)


def test_peercred_wrong_level_never_yields_an_accept(home):
    """The wrong level (SOL_SOCKET) returns 4 zero bytes with no error. Pin
    both halves: our call uses level 0, and the wrong-level answer is refused."""
    wrong = _FakeConn(_xucred())
    got = wrong.getsockopt(socket.SOL_SOCKET, socket.LOCAL_PEERCRED, 76)
    assert got == b"\x00\x00\x00\x00", "the fake must reproduce P3"
    with pytest.raises(struct.error):
        _owner(home)._check_peer_credentials(_FakeConn(raw=got))


def test_peercred_real_socket_wrong_level_is_four_bytes_and_refused(home):
    """Control on the real kernel: the fake above only means something if a real
    socket really answers the wrong level with 4 bytes and no error."""
    path = str(home / "p.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    cli = socket.socket(socket.AF_UNIX)
    cli.connect(path)
    conn, _ = srv.accept()
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.LOCAL_PEERCRED, 76)
        assert len(raw) == 4
        with pytest.raises(struct.error):
            struct.unpack_from("@IIh16I", raw)
        # and the real accepted connection passes the owner's own check (same uid)
        assert _owner(home)._check_peer_credentials(conn) is None
    finally:
        for s in (conn, cli, srv):
            s.close()


@pytest.mark.skipif(os.getuid() == 0, reason="needs a non-root owner")
def test_peercred_uid_zero_is_never_accepted(home):
    rej = _owner(home)._check_peer_credentials(_FakeConn(_xucred(uid=0)))
    assert rej is not None and rej["reason"] == "peer_identity"
    assert "uid 0" in rej["detail"]


@pytest.mark.skipif(os.getuid() == 0, reason="needs a non-root owner")
def test_peercred_uid_mismatch_is_rejected_peer_identity(home):
    own = os.getuid()
    rej = _owner(home)._check_peer_credentials(_FakeConn(_xucred(uid=own + 1)))
    assert rej == {
        "status": "rejected",
        "reason": "peer_identity",
        "detail": f"connecting uid {own + 1} is not this owner's uid {own}",
        "remedy": "connect to the socket under your own $ENGRAM_HOME",
    }


# ---------------------------------------------------------------------------
# Cell 4 -- socket-path length: lives in the shared #3696 contract file
# ---------------------------------------------------------------------------
# tests/test_engramd_socket_path_limit_3696.py runs every length cell on this
# Mac against the measured 103/104 boundary (refusal by name with nothing
# created, at-limit start, bytes-not-characters, kernel re-measure, owner and
# client share one reason text). This file only pins the Darwin constant.


def test_darwin_socket_path_limit_is_the_measured_103():
    assert ec.DARWIN_SOCKET_PATH_MAX_BYTES == 103
    if sys.platform == "darwin":
        assert ec.SOCKET_PATH_MAX_BYTES == 103


# ---------------------------------------------------------------------------
# Cell 5 -- refusal and marker agree, one missing prerequisite at a time
# ---------------------------------------------------------------------------


_PATCHED_PATH = {"/bin/chmod": "/nonexistent-3666/chmod", "/dev/fd": "/nonexistent-3666/fd"}


def _patch_missing(monkeypatch, which: str) -> None:
    """Remove ONE Darwin prerequisite at the primitive the function reads. Never
    `platform.system` (#3375). The chmod / /dev/fd checks read named module
    constants, so pointing them at a path that is not there exercises the real
    existence check."""
    if which == "LOCAL_PEERCRED":
        monkeypatch.delattr(socket, "LOCAL_PEERCRED")
    elif which == "/bin/chmod":
        monkeypatch.setattr(eo, "DARWIN_CHMOD", _PATCHED_PATH[which], raising=False)
    elif which == "/dev/fd":
        monkeypatch.setattr(eo, "DARWIN_DEV_FD", _PATCHED_PATH[which], raising=False)
    else:  # pragma: no cover
        raise AssertionError(which)


def test_a_real_mac_with_every_prerequisite_is_supported():
    assert eo.platform_unsupported_reason() is None


@pytest.mark.parametrize("missing", ["LOCAL_PEERCRED", "/bin/chmod", "/dev/fd"])
def test_refusal_and_marker_agree_when_each_prerequisite_is_missing(missing, home, monkeypatch):
    assert eo.platform_unsupported_reason() is None, "baseline: supported before the patch"
    _patch_missing(monkeypatch, missing)

    reason = eo.platform_unsupported_reason()
    named = _PATCHED_PATH.get(missing, missing)  # the refusal names the missing thing as configured
    assert reason is not None and named in reason, reason
    for other in ("LOCAL_PEERCRED", "/bin/chmod", "/dev/fd"):
        if other != missing:  # and it names only the missing one
            assert _PATCHED_PATH.get(other, other) not in reason, (other, reason)

    # the refusal: start() raises with exactly that text, before touching the disk
    with pytest.raises(eo.OwnerPermissionUnsupported) as exc:
        _owner(home / "h").start(install_signal_handlers=False)
    assert str(exc.value) == reason
    assert not (home / "h").exists()

    # the marker: built from the same function, it skips on exactly this host
    assert op.skip_marker_for(reason).args[0] is True
    assert op.skip_marker_for(None).args[0] is False


def test_the_shared_marker_reads_the_same_function():
    assert op._UNSUPPORTED_REASON == eo.platform_unsupported_reason()
    assert op.requires_supported_owner.args[0] is (eo.platform_unsupported_reason() is not None)


# ---------------------------------------------------------------------------
# Cell 6 -- a TCC-protected ENGRAM_HOME refuses (heuristic prefix list)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_home(home, monkeypatch):
    """A temp 'home' holding Documents/Desktop/Downloads folders. Path.home() is
    patched to it, so the TCC prefix check sees them -- the REAL protected
    folders are never touched (a probe would raise a consent dialog)."""
    uh = home  # the temp dir itself plays ~ (keeps socket paths under 103 bytes)
    for name in ("Documents", "Desktop", "Downloads"):
        (uh / name).mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: uh))
    return uh


@pytest.mark.parametrize("folder", ["Documents", "Desktop", "Downloads"])
def test_home_under_a_tcc_folder_refuses_without_creating_anything(folder, fake_user_home):
    h = fake_user_home / folder / "p" / "e"
    with pytest.raises(eo.OwnerHomeTccProtected) as exc:
        _owner(h).start(install_signal_handlers=False)
    msg = str(exc.value)
    assert "Full Disk Access" in msg and "ENGRAM_HOME" in msg and folder in msg
    assert list((fake_user_home / folder).iterdir()) == [], "nothing created under the folder"


def test_tcc_prefix_match_is_case_insensitive_like_the_default_volume(fake_user_home):
    with pytest.raises(eo.OwnerHomeTccProtected):
        _owner(fake_user_home / "documents" / "x").start(install_signal_handlers=False)


def test_home_outside_the_tcc_folders_starts(fake_user_home):
    for ok in (fake_user_home / "e", fake_user_home / "Documents2" / "e"):
        ok.mkdir(parents=True)
        seed_current_store(ok)  # #3753
        owner = _owner(ok)
        owner.start(install_signal_handlers=False)
        try:
            assert isinstance(ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path), ec.Ok)
        finally:
            owner.stop()


# ---------------------------------------------------------------------------
# Cell 7 -- a full listen backlog is Unavailable-with-a-reason on a Mac
# ---------------------------------------------------------------------------


def test_full_backlog_connect_reads_as_unavailable_with_a_reason(home):
    """Measured on this Mac (macOS 26.6.2): connect() to an AF_UNIX listener whose
    backlog is full fails IMMEDIATELY with ECONNREFUSED (61) -- the same errno as
    a dead owner, unlike Linux where it blocks / EAGAINs. The client's existing
    transport mapping gives Unavailable with the OS reason either way. This cell
    pins only that: it does NOT claim the client can tell busy from down on
    Darwin (it cannot; the errno is identical) -- that is #3690's busy-vs-down
    split and is left to it."""
    path = home / "full.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    held = []
    try:
        refused = None
        for _ in range(16):
            c = socket.socket(socket.AF_UNIX)
            c.settimeout(1.0)
            try:
                c.connect(str(path))
                held.append(c)
            except OSError as exc:
                c.close()
                refused = exc
                break
        assert refused is not None, "positive control: the backlog never filled"
        assert refused.errno == errno.ECONNREFUSED

        result = ec.read("ping", {}, timeout=1.0, socket_path=path)
        assert isinstance(result, ec.Unavailable)
        assert result.detail and "refused" in result.detail.lower()
    finally:
        for c in held:
            c.close()
        srv.close()


# ---------------------------------------------------------------------------
# Cell 8 -- end to end: this closes the issue's noun
# ---------------------------------------------------------------------------


def test_owner_starts_on_this_mac_serves_ping_and_stop_cleans_up(home):
    owner = _owner(home)
    owner.start(install_signal_handlers=False)
    try:
        run = owner.socket_path.parent
        assert stat_mode(run) == 0o700
        assert stat_mode(owner.socket_path) == eo.SOCKET_FILE_MODE
        assert _acl_lines(run) == []

        result = ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path)
        assert isinstance(result, ec.Ok), result
        assert result.result["contract_version"] == ec.CONTRACT_VERSION
    finally:
        owner.stop()

    assert not owner.socket_path.exists(), "stop() must remove the socket"
    after = ec.read("ping", {}, timeout=0.5, socket_path=owner.socket_path)
    assert isinstance(after, ec.Unavailable)


def stat_mode(p: Path) -> int:
    return os.stat(p).st_mode & 0o7777
