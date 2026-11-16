"""Contract tests for the secrets check and its two value-blind helpers [#4842 slice 2].

Written BEFORE the implementation (contract-first, #3045). The contract, from the
issue's items 3 and 4:

  * secrets_check.check(home) -> Result(code, problems, remediation); code 0 pass,
    1 refuse, 2 cannot tell. The directory AND every file under <home>/secrets/ are
    read back through getfacl; an extended ACL, a mask line or a default ACL is a
    defect. Entries are named by basename.
  * secret-env loads named secrets into the environment of ONE child and prints
    nothing; it refuses (child never started) when the check refuses or cannot tell.
  * secret-put writes inside secrets/ (mkstemp(dir=secrets)) so a default ACL on the
    home cannot leak a named entry into a stored secret.

Secrets in these tests are unique marker strings. No assertion message carries a value;
assertions are on booleans. Every home is a tmp_path home, never the real ENGRAM home.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "src" / "engram" / "tools"
CHECK_PY = TOOLS / "secrets_check.py"
SECRET_ENV = TOOLS / "secret-env"
SECRET_PUT = TOOLS / "secret-put"

HAVE_ACL = (
    sys.platform == "linux"
    and shutil.which("setfacl") is not None
    and shutil.which("getfacl") is not None
)
needs_acl = pytest.mark.skipif(not HAVE_ACL, reason="needs Linux setfacl/getfacl")

MARKER = "SECRETMARKER-4842-b7f3c9e1d2a8"
OTHER_UID = str(os.getuid() + 1)  # numeric uid with no account is accepted by setfacl


def _load():
    spec = importlib.util.spec_from_file_location("secrets_check_4842", str(CHECK_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setfacl(*args):
    r = subprocess.run(["setfacl", *args], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("setfacl not usable on this filesystem")


def _write(path: Path, content: str = "x\n", mode: int = 0o600):
    path.write_text(content)
    os.chmod(path, mode)


def _clean_home(tmp_path: Path, files=("alpha-key", "beta.token")) -> Path:
    home = tmp_path / "home"
    sec = home / "secrets"
    sec.mkdir(parents=True)
    os.chmod(sec, 0o700)
    for f in files:
        _write(sec / f, "value-of-" + f + "\n")
    return home


def _run(tool, args, *, env_extra=None, path=None, stdin=None, cwd=None):
    env = dict(os.environ)
    env.pop("ENGRAM_HOME", None)
    if path is not None:
        env["PATH"] = path
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        cwd=cwd,
        timeout=60,
    )


def _check_cli(home):
    return _run(CHECK_PY, ["--home", str(home)])


def _out(r) -> str:
    return r.stdout + r.stderr


# ---------------------------------------------------------------- C1-C8 the check


@needs_acl
def test_c1_clean_passes(tmp_path):
    home = _clean_home(tmp_path)
    assert _load().check(home).code == 0
    assert _check_cli(home).returncode == 0


@needs_acl
def test_c2_named_user_on_dir_refuses_with_remediation(tmp_path):
    home = _clean_home(tmp_path)
    _setfacl("-m", f"u:{OTHER_UID}:r-x", str(home / "secrets"))
    r = _check_cli(home)
    assert r.returncode == 1
    assert "setfacl -b -k" in _out(r)
    assert _load().check(home).code == 1


@needs_acl
def test_c3_mask_line_without_named_entry_refuses(tmp_path):
    home = _clean_home(tmp_path)
    # m::--- keeps st_mode at 0700 (group bits carry the mask), so only the mask LINE can refuse it
    _setfacl("-m", "m::---", str(home / "secrets"))
    assert (os.stat(home / "secrets").st_mode & 0o777) == 0o700
    assert "mask::" in subprocess.run(
        ["getfacl", "-p", str(home / "secrets")], capture_output=True, text=True
    ).stdout
    assert _check_cli(home).returncode == 1


@needs_acl
def test_c4_default_acl_on_dir_refuses(tmp_path):
    home = _clean_home(tmp_path)
    _setfacl("-d", "-m", "u::rwx", str(home / "secrets"))
    assert _check_cli(home).returncode == 1


@needs_acl
def test_c5_file_with_named_entry_renamed_in_refuses_and_names_it(tmp_path):
    home = _clean_home(tmp_path)
    staged = tmp_path / "staged"
    _write(staged, "v\n")
    _setfacl("-m", f"u:{OTHER_UID}:r--", str(staged))
    os.chmod(staged, 0o600)
    os.rename(staged, home / "secrets" / "renamed-in")
    r = _check_cli(home)
    assert r.returncode == 1
    assert "renamed-in" in _out(r)
    assert _load().check(home).code == 1


@needs_acl
def test_c6_dir_mode_reads_0700_but_acl_has_mask_and_named_entry(tmp_path):
    home = _clean_home(tmp_path)
    sec = home / "secrets"
    _setfacl("-m", f"u:{OTHER_UID}:---,m::---", str(sec))
    # the group bits now carry the mask, so st_mode alone looks like 0700
    assert (os.stat(sec).st_mode & 0o777) == 0o700
    assert _check_cli(home).returncode == 1


@needs_acl
def test_c7_file_mode_0644_refuses(tmp_path):
    home = _clean_home(tmp_path)
    os.chmod(home / "secrets" / "alpha-key", 0o644)
    r = _check_cli(home)
    assert r.returncode == 1
    assert "alpha-key" in _out(r)


@needs_acl
def test_c7b_dir_mode_wrong_refuses(tmp_path):
    home = _clean_home(tmp_path)
    os.chmod(home / "secrets", 0o750)
    assert _check_cli(home).returncode == 1


@needs_acl
def test_c8_symlink_refuses_named_and_target_not_read(tmp_path):
    home = _clean_home(tmp_path)
    target = tmp_path / "outside"
    _write(target, MARKER + "\n")
    os.symlink(target, home / "secrets" / "linky")
    r = _check_cli(home)
    assert r.returncode == 1
    assert "linky" in _out(r)
    assert MARKER not in _out(r)


@needs_acl
def test_non_regular_entries_refuse(tmp_path):
    home = _clean_home(tmp_path)
    os.mkdir(home / "secrets" / "subdir")
    os.mkfifo(home / "secrets" / "pipe")
    r = _check_cli(home)
    assert r.returncode == 1
    assert "subdir" in _out(r) and "pipe" in _out(r)


def test_missing_secrets_folder_refuses(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _check_cli(home)
    assert r.returncode == 1
    assert f"no secrets folder at {home}/secrets" in _out(r)


# ---------------------------------------------------------------- C9 cannot tell


@needs_acl
def test_c9_getfacl_absent_is_exit_2_and_secret_env_does_not_start_child(tmp_path):
    home = _clean_home(tmp_path)
    empty = tmp_path / "emptybin"
    empty.mkdir()
    r = _run(CHECK_PY, ["--home", str(home)], path=str(empty))
    assert r.returncode == 2
    assert "cannot tell" in _out(r)
    sentinel = tmp_path / "ran"
    r2 = _run(
        SECRET_ENV,
        ["--home", str(home), "alpha-key", "--", sys.executable, "-c",
         f"open({str(sentinel)!r}, 'w').close()"],
        path=str(empty),
    )
    assert r2.returncode == 2
    assert "cannot tell" in _out(r2)
    assert not sentinel.exists()


def _shim_path(tmp_path, body):
    shim = tmp_path / "shimbin"
    shim.mkdir()
    g = shim / "getfacl"
    g.write_text("#!/bin/sh\n" + body + "\n")
    os.chmod(g, 0o755)
    return str(shim) + os.pathsep + os.environ["PATH"]


@needs_acl
@pytest.mark.parametrize("body", ["exit 1", "exit 0"])
def test_c9b_getfacl_failing_or_empty_is_exit_2(tmp_path, body):
    home = _clean_home(tmp_path)
    r = _run(CHECK_PY, ["--home", str(home)], path=_shim_path(tmp_path, body))
    assert r.returncode == 2
    assert "cannot tell" in _out(r)


# ---------------------------------------------------------------- C10-C13 secret-env


def _marker_home(tmp_path, content=MARKER + "\n"):
    home = _clean_home(tmp_path, files=())
    _write(home / "secrets" / "anthropic-api-key.acct", content)
    return home


@needs_acl
def test_c10_secret_env_never_echoes(tmp_path):
    home = _marker_home(tmp_path)
    r = _run(
        SECRET_ENV,
        ["--home", str(home), "anthropic-api-key.acct", "--", sys.executable, "-c",
         "import sys; sys.exit(1)"],
    )
    assert r.returncode == 1
    assert r.stdout == "" and r.stderr == ""
    assert MARKER not in _out(r)


@needs_acl
def test_c11_child_sees_value_and_name_mapping(tmp_path):
    home = _marker_home(tmp_path)
    child = (
        "import os;"
        "v=os.environ.get('ANTHROPIC_API_KEY_ACCT');"
        "print('set' if v is not None else 'unset');"
        f"print('match' if v == {MARKER!r} else 'differ')"
    )
    r = _run(SECRET_ENV, ["--home", str(home), "anthropic-api-key.acct", "--", sys.executable, "-c", child])
    assert r.returncode == 0
    assert r.stdout.split() == ["set", "match"]
    assert MARKER not in r.stderr


@needs_acl
def test_c11b_as_override_and_one_newline_only(tmp_path):
    home = _marker_home(tmp_path, content=MARKER + "\n\n")
    child = (
        "import os;v=os.environ.get('MYKEY');"
        f"print('match' if v == {MARKER + chr(10)!r} else 'differ')"
    )
    r = _run(
        SECRET_ENV,
        ["--home", str(home), "--as", "MYKEY=anthropic-api-key.acct", "anthropic-api-key.acct",
         "--", sys.executable, "-c", child],
    )
    assert r.returncode == 0
    assert r.stdout.split() == ["match"]


@needs_acl
def test_c12_secret_env_refuses_when_check_refuses(tmp_path):
    home = _marker_home(tmp_path)
    os.chmod(home / "secrets" / "anthropic-api-key.acct", 0o644)
    sentinel = tmp_path / "ran"
    r = _run(
        SECRET_ENV,
        ["--home", str(home), "anthropic-api-key.acct", "--", sys.executable, "-c",
         f"open({str(sentinel)!r}, 'w').close()"],
    )
    assert r.returncode == 1
    assert "anthropic-api-key.acct" in _out(r)
    assert not sentinel.exists()
    assert MARKER not in _out(r)


@needs_acl
def test_c13_missing_secret_names_it_exit_1(tmp_path):
    home = _marker_home(tmp_path)
    sentinel = tmp_path / "ran"
    r = _run(
        SECRET_ENV,
        ["--home", str(home), "no-such-secret", "--", sys.executable, "-c",
         f"open({str(sentinel)!r}, 'w').close()"],
    )
    assert r.returncode == 1
    assert "no-such-secret" in _out(r)
    assert MARKER not in _out(r)
    assert not sentinel.exists()


@pytest.mark.parametrize("bad", ["../x", "a/b", ".hidden", ""])
def test_name_validation_refuses(tmp_path, bad):
    home = _marker_home(tmp_path) if HAVE_ACL else tmp_path
    r = _run(SECRET_ENV, ["--home", str(home), bad, "--", sys.executable, "-c", "pass"])
    assert r.returncode == 1
    assert "must match" in _out(r)
    r2 = _run(SECRET_PUT, ["--home", str(home), bad], stdin="v\n")
    assert r2.returncode == 1
    assert "must match" in _out(r2)


@needs_acl
def test_non_utf8_secret_refuses_without_content(tmp_path):
    home = _clean_home(tmp_path, files=())
    p = home / "secrets" / "binary"
    p.write_bytes(b"\xff\xfe" + MARKER.encode())
    os.chmod(p, 0o600)
    r = _run(SECRET_ENV, ["--home", str(home), "binary", "--", sys.executable, "-c", "pass"])
    assert r.returncode == 1
    assert "binary" in _out(r)
    assert MARKER not in _out(r)


# ---------------------------------------------------------------- C14 secret-put


@needs_acl
def test_c14_put_stays_inside_secrets_so_home_default_acl_does_not_leak(tmp_path):
    home = _clean_home(tmp_path, files=())
    _setfacl("-d", "-m", f"u:{OTHER_UID}:rwx", str(home))
    # secrets/ exists and is clean (strip what it inherited from the home)
    _setfacl("-b", "-k", str(home / "secrets"))
    r = _run(SECRET_PUT, ["--home", str(home), "new-key"], stdin=MARKER + "\n")
    assert r.returncode == 0
    assert MARKER not in _out(r)
    f = home / "secrets" / "new-key"
    assert f.read_text() == MARKER  # one trailing newline stripped
    assert (os.stat(f).st_mode & 0o777) == 0o600
    assert _load().check(home).code == 0


@needs_acl
def test_put_into_missing_secrets_creates_0700_base_acl_despite_home_default_acl(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _setfacl("-d", "-m", f"u:{OTHER_UID}:rwx", str(home))
    r = _run(SECRET_PUT, ["--home", str(home), "fresh"], stdin="v\n")
    assert r.returncode == 0
    sec = home / "secrets"
    assert (os.stat(sec).st_mode & 0o777) == 0o700
    assert _load().check(home).code == 0


@needs_acl
def test_put_refuses_existing_dirty_folder_and_does_not_repair(tmp_path):
    home = _clean_home(tmp_path, files=())
    _setfacl("-m", f"u:{OTHER_UID}:r-x", str(home / "secrets"))
    r = _run(SECRET_PUT, ["--home", str(home), "k"], stdin="v\n")
    assert r.returncode == 1
    assert "setfacl -b -k" in _out(r)
    assert not (home / "secrets" / "k").exists()
    assert _load().check(home).code == 1  # still dirty: not silently repaired


@needs_acl
def test_put_overwrites_atomically_and_leaves_no_temp(tmp_path):
    home = _clean_home(tmp_path, files=())
    assert _run(SECRET_PUT, ["--home", str(home), "k"], stdin="one\n").returncode == 0
    assert _run(SECRET_PUT, ["--home", str(home), "k"], stdin="two\n").returncode == 0
    assert (home / "secrets" / "k").read_text() == "two"
    assert sorted(p.name for p in (home / "secrets").iterdir()) == ["k"]


# ---------------------------------------------------------------- C15 non-Linux


def test_c15_non_linux_prints_note_and_still_enforces_modes(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path, files=("good",))
    monkeypatch.setattr(sys, "platform", "darwin")
    res = mod.check(home)
    assert res.code == 0
    assert capsys.readouterr().err.splitlines().count("secrets ACL not checked on this platform") == 1

    _write(home / "secrets" / "loose", "v\n", mode=0o644)
    res = mod.check(home)
    assert res.code == 1
    err = capsys.readouterr().err
    assert "secrets ACL not checked on this platform" in err
    assert any("loose" in p for p in res.problems)


# ---------------------------------------------------------------- round 2 (R1-R7)

import importlib.machinery  # noqa: E402
import io  # noqa: E402
import shlex  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402


def _load_tool(path, modname):
    """Load an extension-less tool as a module (its __main__ guard keeps main() from running)."""
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.path)  # the tools put their own directory on sys.path at import (#4023 guard)
    try:
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


@needs_acl
def test_r1_remediation_lines_are_shell_quoted(tmp_path):
    home = tmp_path / "ho me's"  # a space and a quote: an unquoted line cannot survive shlex
    sec = home / "secrets"
    sec.mkdir(parents=True)
    os.chmod(sec, 0o700)
    f = sec / "loose-key"
    _write(f, "v\n")
    _setfacl("-m", f"u:{OTHER_UID}:r-x", str(sec))
    _setfacl("-m", f"u:{OTHER_UID}:r--", str(f))
    r = _check_cli(home)
    assert r.returncode == 1
    split = [
        shlex.split(ln.strip())
        for ln in r.stdout.splitlines()
        if ln.strip().startswith(("setfacl ", "chmod "))
    ]
    assert ["setfacl", "-b", "-k", str(sec)] in split
    assert ["setfacl", "-b", str(f)] in split
    chmods = [s for s in split if s[0] == "chmod"]
    assert chmods and all(len(s) == 3 and s[2] in (str(sec), str(f)) for s in chmods)


@needs_acl
def test_r2_getfacl_gets_double_dash_so_a_dash_leading_relative_home_works(tmp_path):
    sec = tmp_path / "-dashhome" / "home" / "secrets"
    sec.mkdir(parents=True)
    os.chmod(sec, 0o700)
    _write(sec / "k", "v\n")
    r = _run(CHECK_PY, ["--home", "-dashhome/home"], cwd=str(tmp_path))
    assert r.returncode == 0


def test_r3_put_validates_stdin_before_creating_secrets_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for stdin in ("", "\n", "a\x00b\n"):
        r = _run(SECRET_PUT, ["--home", str(home), "k"], stdin=stdin)
        assert r.returncode == 1
        assert not (home / "secrets").exists()


@needs_acl
def test_r4_put_fsyncs_the_secrets_dir_after_replace(tmp_path, monkeypatch):
    mod = _load_tool(SECRET_PUT, "secret_put_r4")
    home = _clean_home(tmp_path, files=())
    sec_ino = os.stat(home / "secrets").st_ino
    events = []
    real_replace, real_fsync = os.replace, os.fsync

    def spy_replace(a, b, *args, **kw):
        events.append("replace")
        return real_replace(a, b, *args, **kw)

    def spy_fsync(fd):
        st = os.fstat(fd)
        events.append("fsync-dir" if stat.S_ISDIR(st.st_mode) and st.st_ino == sec_ino else "fsync-other")
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"v\n")))
    assert mod.main(["--home", str(home), "k"]) == 0
    assert "replace" in events and "fsync-dir" in events
    assert events.index("fsync-dir") > events.index("replace")


@needs_acl
def test_f2_dir_fsync_failure_is_reported_not_a_traceback(tmp_path, monkeypatch, capsys):
    mod = _load_tool(SECRET_PUT, "secret_put_f2")
    home = _clean_home(tmp_path, files=())
    rechecks = []
    real_check = mod.secrets_check.check

    def spy_check(h):
        rechecks.append(h)
        return real_check(h)

    def boom(path):
        raise OSError(5, "boom")

    monkeypatch.setattr(mod, "_fsync_dir", boom)
    monkeypatch.setattr(mod.secrets_check, "check", spy_check)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"v-f2\n")))
    rc = mod.main(["--home", str(home), "k"])
    err = capsys.readouterr().err
    assert rc == 1
    assert ("stored, but the secrets folder could not be synced to disk; "
            "the value may not survive a crash") in err
    assert "Traceback" not in err and "v-f2" not in err
    f = home / "secrets" / "k"
    assert f.exists() and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert len(rechecks) == 2  # pre-write check and the post-write re-check


def _r5_refused(tmp_path, args, names_to_make, var):
    home = _clean_home(tmp_path, files=())
    for n in names_to_make:
        _write(home / "secrets" / n, "x-" + n + "\n")
    sentinel = tmp_path / "ran"
    r = _run(
        SECRET_ENV,
        ["--home", str(home), *args, "--", sys.executable, "-c",
         f"open({str(sentinel)!r}, 'w').close()"],
    )
    assert r.returncode == 1
    assert not sentinel.exists()
    assert var in _out(r)
    assert "would override a variable that changes how programs start" in _out(r)
    assert "x-" not in _out(r)


@needs_acl
def test_r5_default_mapping_to_path_refused(tmp_path):
    _r5_refused(tmp_path, ["path"], ["path"], "PATH")


@needs_acl
def test_r5_default_mapping_with_dash_refused(tmp_path):
    _r5_refused(tmp_path, ["bash-env"], ["bash-env"], "BASH_ENV")


@needs_acl
@pytest.mark.parametrize(
    "var",
    ["LD_PRELOAD", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "BASH_FUNC_FOO", "NODE_OPTIONS", "HOME", "PS4"],
)
def test_r5_as_override_to_dangerous_var_refused(tmp_path, var):
    _r5_refused(tmp_path, ["--as", f"{var}=k", "k"], ["k"], var)


@needs_acl
@pytest.mark.parametrize(
    "var",
    ["GIT_SSH_COMMAND", "LC_ALL", "JAVA_TOOL_OPTIONS", "HTTPS_PROXY", "GIT_CONFIG_COUNT", "CARGO_HOME",
     "SHELLOPTS", "SSL_CERT_FILE", "EDITOR", "NPM_CONFIG_REGISTRY", "GOFLAGS"],
)
def test_f1_round3_added_startup_vars_refused_and_message_says_how_to_proceed(tmp_path, var):
    _r5_refused(tmp_path, ["--as", f"{var}=k", "k"], ["k"], var)
    home = tmp_path / "home"
    r = _run(SECRET_ENV, ["--home", str(home), "--as", f"{var}=k", "k", "--", sys.executable, "-c", "pass"])
    assert "choose another variable with --as VAR=NAME" in _out(r)


@needs_acl
def test_f1_round3_default_mapping_via_python_prefix_message_names_as(tmp_path):
    _r5_refused(tmp_path, ["python-token"], ["python-token"], "PYTHON_TOKEN")
    r = _run(SECRET_ENV, ["--home", str(tmp_path / "home"), "python-token", "--", sys.executable, "-c", "pass"])
    assert "--as" in _out(r)


def test_f1_round3_prefix_and_name_sets_cover_the_additions():
    mod = _load_tool(SECRET_ENV, "secret_env_f1")
    for v in ("GIT_CONFIG_KEY_0", "CARGO_HOME", "NPM_CONFIG_PREFIX", "LC_MESSAGES"):
        assert mod.is_startup_var(v)
    assert not mod.is_startup_var("ANTHROPIC_API_KEY")


@needs_acl
def test_f3_name_to_var_mapping_computed_once(tmp_path, monkeypatch):
    mod = _load_tool(SECRET_ENV, "secret_env_f3")
    home = _clean_home(tmp_path, files=())
    _write(home / "secrets" / "tok", "v\n")
    calls = []
    real = mod.default_var
    monkeypatch.setattr(mod, "default_var", lambda n: calls.append(n) or real(n))
    monkeypatch.setattr(mod.os, "execvpe", lambda *a, **k: (_ for _ in ()).throw(OSError("stop")))
    assert mod.main(["--home", str(home), "tok", "--", "true"]) == 127
    assert calls == ["tok"]


@needs_acl
def test_r5_ordinary_name_still_passes(tmp_path):
    home = _clean_home(tmp_path, files=())
    _write(home / "secrets" / "anthropic-api-key", "v\n")
    r = _run(
        SECRET_ENV,
        ["--home", str(home), "anthropic-api-key", "--", sys.executable, "-c",
         "import os;print('set' if 'ANTHROPIC_API_KEY' in os.environ else 'unset')"],
    )
    assert r.returncode == 0
    assert r.stdout.split() == ["set"]


def test_r6_read_secret_refuses_a_non_regular_file_without_blocking(tmp_path):
    mod = _load_tool(SECRET_ENV, "secret_env_r6")
    fifo = tmp_path / "pipe-secret"
    os.mkfifo(fifo)
    reg = tmp_path / "plain"
    reg.write_text(MARKER + "\n")

    class _Blocked(BaseException):  # not an OSError/Exception, so the helper cannot swallow it
        pass

    def _timeout(signum, frame):
        raise _Blocked()

    old = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(10)
    refused = named = False
    try:
        try:
            mod.read_secret(fifo)
        except Exception as exc:  # the helper's own refusal type
            refused = "not a regular file" in str(exc)
            named = "pipe-secret" in str(exc) and MARKER not in str(exc)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert refused
    assert named
    assert mod.read_secret(reg) == MARKER  # a regular file still reads (one newline dropped)


def _home_env(tmp_path, engram_home):
    fake = tmp_path / "fakehome"
    fake.mkdir(exist_ok=True)
    return {"HOME": str(fake), "ENGRAM_HOME": str(engram_home)}


@needs_acl
def test_r7_default_home_comes_from_engram_home(tmp_path):
    home = _marker_home(tmp_path)
    env = _home_env(tmp_path, home)
    assert _run(CHECK_PY, [], env_extra=env).returncode == 0
    r = _run(
        SECRET_ENV,
        ["anthropic-api-key.acct", "--", sys.executable, "-c",
         "import os;print('set' if 'ANTHROPIC_API_KEY_ACCT' in os.environ else 'unset')"],
        env_extra=env,
    )
    assert r.returncode == 0
    assert r.stdout.split() == ["set"]
    r = _run(SECRET_PUT, ["put-via-env"], stdin="v\n", env_extra=env)
    assert r.returncode == 0
    assert (home / "secrets" / "put-via-env").exists()
    assert not (tmp_path / "fakehome" / ".engram").exists()


@needs_acl
def test_r7_relative_engram_home_refused_by_all_three(tmp_path):
    env = _home_env(tmp_path, "relative/home")
    outs = [
        _run(CHECK_PY, [], env_extra=env),
        _run(SECRET_ENV, ["anthropic-api-key.acct", "--", sys.executable, "-c", "pass"], env_extra=env),
        _run(SECRET_PUT, ["k"], stdin="v\n", env_extra=env),
    ]
    for r in outs:
        assert r.returncode == 1
        assert "ENGRAM_HOME must be an absolute path" in _out(r)
    assert not (tmp_path / "fakehome" / ".engram").exists()
