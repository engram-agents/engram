"""Contract tests for the run/ ACL read-back (#3698, part of #2652) -- the
Linux half.

The contract (written before the implementation):

  A1  after the `setfacl -b` strip, _harden_run_dir() reads the ACL back
      through the SAME fd (`getfacl -c -n /proc/self/fd/<fd>`), never the path,
      before the fd is closed and before the stale-socket check and the bind.
  A2  the only accepted entries are exactly one each of `user::`, `group::`,
      `other::`. Blank lines and `#` comment lines are ignored. Anything else
      (named user/group, `mask::`, any `default:` line, an unrecognised line, a
      duplicated or missing base entry) is a leftover and refuses.
  A3  the refusal is a named RuntimeError, OwnerAclNotStripped, carrying the
      run_dir path and the leftover entries verbatim (listing capped at 10,
      with the count of the rest). A getfacl failure (non-zero exit, timeout,
      missing binary at run time, unparseable output) also refuses, with its
      own reason. A missing `getfacl` is refused by _check_platform_support()
      like a missing `setfacl`. main() prints the message and returns 1.
  A4  healthy paths do not change: a real strip under a parent that carries an
      inheritable default ACL still starts and serves.
  A5  the read-back is the helper `_read_acl_entries(fd)`; the judge is
      `_acl_leftovers(entries)`.

A real `setfacl -b` never leaves an entry, so the refusal cells can only be
shown by making the strip a no-op (or by stubbing getfacl's output); the
docstrings below say so where it applies.

Environment pinning: every test builds its own directory under a fresh
tempfile.mkdtemp() and passes it to EngramdOwner explicitly; the ambient
ENGRAM_HOME / HOME are never read (test_main_* sets ENGRAM_HOME itself). Tests
that need filesystem ACLs SKIP with a reason when `setfacl -m` fails on the
fixture (tmp filesystem without ACL support) or when setfacl/getfacl are not
on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _owner_platform import requires_linux_kernel, requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

REQUEST_DEADLINE = 0.6
_NOBODY = "65534"  # numeric uid; never resolved by name
_REAL_RUN = subprocess.run

_needs_acl_tools = pytest.mark.skipif(
    shutil.which("setfacl") is None or shutil.which("getfacl") is None,
    reason="setfacl/getfacl not on PATH",
)


@pytest.fixture
def tmp_base():
    d = tempfile.mkdtemp(prefix="acl-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _setfacl(*args: str) -> None:
    p = _REAL_RUN(["setfacl", *args], capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip(f"fixture filesystem does not support ACLs: {p.stderr.strip()}")


def _getfacl(path) -> list[str]:
    p = _REAL_RUN(["getfacl", "-c", "-n", str(path)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def _skip_if_socket_path_too_long(home: Path) -> None:
    """start()/main() refuse an over-long socket path first (#3696). With a very long TMPDIR
    that is the tmp base's fault, not this file's subject, so skip rather than fail."""
    n = len(os.fsencode(home / "run" / "engramd.sock"))
    if n > ec.SOCKET_PATH_MAX_BYTES:
        pytest.skip(f"tmp base too long for start(): socket path is {n} bytes, limit {ec.SOCKET_PATH_MAX_BYTES}")


def _owner(home: Path) -> "eo.EngramdOwner":
    return eo.EngramdOwner(home, request_deadline=REQUEST_DEADLINE)


def _home_with_run(base: Path, *acl_args: str) -> tuple[Path, Path]:
    """home/run exists and carries the ACL entry given by `acl_args`."""
    home = base / "h"
    run = home / "run"
    run.mkdir(parents=True)
    _setfacl(*acl_args, str(run))
    return home, run


def _noop_strip(monkeypatch):
    """Make the `setfacl -b` strip a no-op that still exits 0; let every other
    subprocess call (the getfacl read-back) through."""

    def fake(argv, *a, **k):
        if argv and argv[0] == "setfacl":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return _REAL_RUN(argv, *a, **k)

    monkeypatch.setattr(eo.subprocess, "run", fake)


def _stub_getfacl(monkeypatch, *, stdout="", stderr="", returncode=0, exc=None):
    """Real setfacl, stubbed getfacl."""
    seen = []

    def fake(argv, *a, **k):
        if argv and argv[0] == "getfacl":
            seen.append(list(argv))
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        return _REAL_RUN(argv, *a, **k)

    monkeypatch.setattr(eo.subprocess, "run", fake)
    return seen


CLEAN = "user::rwx\ngroup::r-x\nother::r-x\n\n"


# -- A5: the judge (pure) -------------------------------------------------------


def test_judge_accepts_exactly_the_base_triple_any_bits():
    assert eo._acl_leftovers(["user::rwx", "group::r-x", "other::r-x"]) == []
    assert eo._acl_leftovers(["user::---", "group::---", "other::---"]) == []


@pytest.mark.parametrize(
    "extra",
    [
        "user:65534:rwx",
        "group:65534:r-x",
        "mask::rwx",
        "default:user::rwx",
        "default:user:65534:rwx",
        "default:mask::rwx",
        "bogus line",
    ],
)
def test_judge_flags_each_leftover_shape(extra):
    left = eo._acl_leftovers(["user::rwx", "group::r-x", "other::r-x", extra])
    assert left == [extra]


def test_judge_mask_only_line_is_a_leftover():
    # a stub getfacl output whose only extra is mask:: -- must refuse
    assert eo._acl_leftovers(["user::rwx", "mask::rwx", "group::r-x", "other::r-x"]) == [
        "mask::rwx"
    ]


def test_judge_missing_and_duplicate_base_entries_are_leftovers():
    assert eo._acl_leftovers(["user::rwx", "group::r-x"]) != []
    assert eo._acl_leftovers(["user::rwx", "user::rwx", "group::r-x", "other::r-x"]) != []
    assert eo._acl_leftovers([]) != []


def test_judge_a_named_entry_is_never_mistaken_for_the_base_entry():
    # Real ACLs list the base triple next to every named entry, so the duplicate-count check
    # would catch a named entry even if the base pattern wrongly accepted it. The pattern is
    # still pinned on its own: a named entry with no base line is a leftover AND leaves the
    # base entry missing.
    left = eo._acl_leftovers(["user:65534:rwx", "group::r-x", "other::r-x"])
    assert "user:65534:rwx" in left
    assert any("missing base entry user" in x for x in left)


def test_judge_single_leftover_is_not_forgiven():
    # off-by-one guard: ONE stray entry must be reported, not only two or more
    left = eo._acl_leftovers(["user::rwx", "group::r-x", "other::r-x", "default:other::r-x"])
    assert len(left) == 1


# -- T1: no-op strip over a real entry -> named refusal -------------------------


@requires_linux_owner
@_needs_acl_tools
@pytest.mark.parametrize(
    "acl_args,needle",
    [
        pytest.param(["-m", f"u:{_NOBODY}:rwx"], f"user:{_NOBODY}:", id="named-user"),
        pytest.param(["-d", "-m", f"u:{_NOBODY}:rwx"], "default:user:", id="default-only"),
        pytest.param(["-m", f"g:{_NOBODY}:r-x"], f"group:{_NOBODY}:", id="named-group"),
    ],
)
def test_noop_strip_over_a_real_entry_is_refused_by_name(tmp_base, monkeypatch, acl_args, needle):
    """MUTATION-SHAPED BY NECESSITY: a real `setfacl -b` never leaves an entry,
    so the leftover can only be produced by making the strip a no-op (exit 0)."""
    home, run = _home_with_run(tmp_base, *acl_args)
    _skip_if_socket_path_too_long(home)
    _noop_strip(monkeypatch)
    owner = _owner(home)
    try:
        with pytest.raises(eo.OwnerAclNotStripped) as ei:
            owner.start(install_signal_handlers=False)
        bound = owner.socket_path.exists()
    finally:
        owner.stop()  # a pre-fix start() succeeds; never leave its threads running
    msg = str(ei.value)
    assert isinstance(ei.value, RuntimeError)
    assert str(run) in msg
    assert needle in msg
    assert not bound, "socket was bound despite the refusal"


@requires_linux_owner
@_needs_acl_tools
def test_refusal_lists_at_most_ten_leftovers_and_counts_the_rest(tmp_base, monkeypatch):
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    extra = [f"user:{1000 + i}:rwx" for i in range(14)]
    _stub_getfacl(
        monkeypatch, stdout="user::rwx\ngroup::r-x\nother::r-x\n" + "\n".join(extra) + "\n"
    )
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    msg = str(ei.value)
    for e in extra[:10]:
        assert e in msg
    for e in extra[10:]:
        assert e not in msg
    assert "4 more" in msg


@requires_linux_owner
@_needs_acl_tools
@pytest.mark.parametrize("extra", ["default:other::r-x", "mask::rwx", "user:1000:rwx"])
def test_exactly_one_leftover_entry_still_refuses(tmp_base, monkeypatch, extra):
    """Off-by-one guard at the refusal site: a SINGLE stray entry refuses."""
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    _stub_getfacl(monkeypatch, stdout=CLEAN.rstrip("\n") + "\n" + extra + "\n")
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert extra in str(ei.value)
    assert "more)" not in str(ei.value)


# -- T2: healthy path, parent with an inheritable default ACL -------------------


@requires_linux_owner
@_needs_acl_tools
def test_noop_strip_on_a_freshly_created_run_dir_under_a_default_acl_parent_is_refused(tmp_base, monkeypatch):
    """The #3698 scenario itself: run/ does not exist, the parent carries a default ACL, so the
    fresh run/ INHERITS entries. With the strip a no-op they remain and must be caught. Every
    other refusal cell pre-creates run/, which would not notice a check skipped on fresh dirs."""
    home = tmp_base / "h"
    home.mkdir()
    _setfacl("-d", "-m", f"u:{_NOBODY}:rwx", str(home))
    _noop_strip(monkeypatch)
    assert not (home / "run").exists()
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert (home / "run").is_dir()
    assert f"user:{_NOBODY}:" in str(ei.value)


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@requires_linux_owner
@requires_linux_kernel  # counts fds via /proc/self/fd and drives the setfacl strip (#3666 marker audit)
@pytest.mark.parametrize(
    "stub",
    [
        pytest.param(dict(exc=subprocess.TimeoutExpired(["getfacl"], 30)), id="timeout"),
        pytest.param(dict(exc=FileNotFoundError("getfacl")), id="cannot-run"),
        pytest.param(dict(returncode=2, stderr="boom"), id="nonzero-exit"),
        pytest.param(dict(stdout=""), id="no-entries"),
        pytest.param(dict(stdout=CLEAN + "mask::rwx\n"), id="leftover"),
    ],
)
def test_every_refusal_closes_the_run_dir_fd(tmp_base, monkeypatch, stub):
    """A refused start must not leak the run/ fd the strip and the read-back used."""
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    _stub_getfacl(monkeypatch, **stub)
    before = _open_fd_count()
    with pytest.raises(eo.OwnerAclNotStripped):
        _owner(home)._harden_run_dir()
    assert _open_fd_count() == before, "the run/ directory fd was not closed after a refusal"


@requires_linux_owner
@_needs_acl_tools
def test_healthy_real_strip_under_default_acl_parent_starts_and_serves(tmp_base):
    home = tmp_base / "h"
    home.mkdir()
    _skip_if_socket_path_too_long(home)
    _setfacl("-d", "-m", f"u:{_NOBODY}:rwx", str(home))
    probe = home / "probe"
    probe.mkdir()
    if not any(ln.startswith("default:") for ln in _getfacl(probe)):
        pytest.skip("parent default ACL is not inherited on this filesystem")
    owner = _owner(home)
    owner.start(install_signal_handlers=False)
    try:
        assert eo._acl_leftovers(_getfacl(home / "run")) == []
        assert isinstance(ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path), ec.Ok)
    finally:
        owner.stop()


# -- T3: swap control -----------------------------------------------------------


@_needs_acl_tools
def test_readback_judges_the_fds_directory_not_the_path(tmp_base):
    run = tmp_base / "run"
    run.mkdir()
    fd = os.open(str(run), os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert _REAL_RUN(["setfacl", "-b", f"/proc/self/fd/{fd}"], pass_fds=[fd]).returncode == 0
        run.rename(tmp_base / "run.moved")
        run.mkdir()
        _setfacl("-m", f"u:{_NOBODY}:rwx", str(run))
        assert eo._acl_leftovers(_getfacl(run)) != [], "decoy carries no entry"
        assert eo._acl_leftovers(eo._read_acl_entries(fd)) == []
    finally:
        os.close(fd)


@requires_linux_owner
@_needs_acl_tools
def test_swap_between_strip_and_readback_inside_harden_is_judged_by_fd(tmp_base, monkeypatch):
    """Inside _harden_run_dir(): right after the real strip returns, swap run/
    for a decoy carrying an entry. Reading by fd judges the stripped original
    (passes); reading by path would judge the decoy (would refuse)."""
    home = tmp_base / "h"
    run = home / "run"
    run.mkdir(parents=True)

    def fake(argv, *a, **k):
        res = _REAL_RUN(argv, *a, **k)
        if argv and argv[0] == "setfacl":
            run.rename(home / "run.moved")
            run.mkdir()
            _setfacl("-m", f"u:{_NOBODY}:rwx", str(run))
        return res

    monkeypatch.setattr(eo.subprocess, "run", fake)
    _owner(home)._harden_run_dir()  # must not raise


# -- T4: getfacl failures refuse -------------------------------------------------


@requires_linux_owner
@_needs_acl_tools
@pytest.mark.parametrize(
    "kw,needle",
    [
        pytest.param({"returncode": 2, "stderr": "boom"}, "exit 2", id="nonzero-exit"),
        pytest.param({"exc": subprocess.TimeoutExpired(["getfacl"], 30)}, "timed out", id="timeout"),
        pytest.param({"exc": FileNotFoundError("getfacl")}, "getfacl", id="vanished"),
        pytest.param({"stdout": "\x00\x01 not acl text\n"}, "leftover", id="garbage"),
        pytest.param({"stdout": ""}, "unparseable", id="empty"),
    ],
)
def test_getfacl_failure_refuses_with_its_own_reason(tmp_base, monkeypatch, kw, needle):
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    _stub_getfacl(monkeypatch, **kw)
    with pytest.raises(eo.OwnerAclNotStripped) as ei:
        _owner(home)._harden_run_dir()
    assert needle in str(ei.value)
    assert str(home / "run") in str(ei.value)


@requires_linux_owner
@_needs_acl_tools
def test_getfacl_is_called_with_a_proc_fd_path(tmp_base, monkeypatch):
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    seen = _stub_getfacl(monkeypatch, stdout=CLEAN)
    _owner(home)._harden_run_dir()
    assert len(seen) == 1
    assert seen[0][:3] == ["getfacl", "-c", "-n"]
    assert seen[0][3].startswith("/proc/self/fd/")


# -- T5: a stderr warning alone does not refuse ---------------------------------


@requires_linux_owner
@_needs_acl_tools
def test_stderr_warning_with_clean_stdout_does_not_refuse(tmp_base, monkeypatch):
    home = tmp_base / "h"
    (home / "run").mkdir(parents=True)
    _stub_getfacl(
        monkeypatch, stdout=CLEAN, stderr="getfacl: Removing leading '/' from absolute path names\n"
    )
    _owner(home)._harden_run_dir()  # must not raise


# -- T6: missing getfacl is a platform refusal ----------------------------------


def _which_without(missing: str):
    return lambda name, *a, **k: None if name == missing else "/usr/bin/" + name


def test_platform_reason_names_a_missing_getfacl(monkeypatch):
    monkeypatch.setattr(eo.platform, "system", lambda: "Linux")
    monkeypatch.setattr(eo.shutil, "which", _which_without("getfacl"))
    monkeypatch.setattr(eo.socket, "SO_PEERCRED", 17, raising=False)
    reason = eo.platform_unsupported_reason()
    assert reason is not None and "getfacl" in reason


def test_start_refuses_a_missing_getfacl_before_creating_anything(tmp_base, monkeypatch):
    monkeypatch.setattr(eo.platform, "system", lambda: "Linux")
    monkeypatch.setattr(eo.shutil, "which", _which_without("getfacl"))
    monkeypatch.setattr(eo.socket, "SO_PEERCRED", 17, raising=False)
    home = tmp_base / "fresh"
    with pytest.raises(eo.OwnerPermissionUnsupported) as ei:
        _owner(home).start(install_signal_handlers=False)
    assert "getfacl" in str(ei.value)
    assert not home.exists()


# -- T7: main() ------------------------------------------------------------------


@requires_linux_owner
@_needs_acl_tools
def test_main_exits_nonzero_with_message_on_leftover_acl(tmp_base, monkeypatch, capsys):
    home, run = _home_with_run(tmp_base, "-m", f"u:{_NOBODY}:rwx")
    _skip_if_socket_path_too_long(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    _noop_strip(monkeypatch)

    def _no_serve(self):  # a pre-fix start() succeeds; never block forever
        self.stop()
        raise AssertionError("owner started despite the leftover ACL")

    monkeypatch.setattr(eo.EngramdOwner, "wait_forever", _no_serve)
    assert eo.main() == 1
    err = capsys.readouterr().err
    assert err.startswith("[engramd] ")
    assert str(run) in err and f"user:{_NOBODY}:" in err
