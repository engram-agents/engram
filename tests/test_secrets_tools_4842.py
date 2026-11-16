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
import re
import shlex
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
# Cells that themselves call setfacl/getfacl (or exercise the Linux-only getfacl path): Linux only.
needs_acl = pytest.mark.skipif(not HAVE_ACL, reason="needs Linux setfacl/getfacl")
# Cells that only run the tools: on Linux the tools need getfacl; on darwin they use ls; so a Mac
# run covers them. Skipped only on a Linux box without getfacl.
needs_backend = pytest.mark.skipif(
    sys.platform == "linux" and shutil.which("getfacl") is None,
    reason="the Linux check needs getfacl",
)

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


@needs_backend
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


@needs_backend
def test_c7_file_mode_0644_refuses(tmp_path):
    home = _clean_home(tmp_path)
    os.chmod(home / "secrets" / "alpha-key", 0o644)
    r = _check_cli(home)
    assert r.returncode == 1
    assert "alpha-key" in _out(r)


@needs_backend
def test_c7b_dir_mode_wrong_refuses(tmp_path):
    home = _clean_home(tmp_path)
    os.chmod(home / "secrets", 0o750)
    assert _check_cli(home).returncode == 1


@needs_backend
def test_c8_symlink_refuses_named_and_target_not_read(tmp_path):
    home = _clean_home(tmp_path)
    target = tmp_path / "outside"
    _write(target, MARKER + "\n")
    os.symlink(target, home / "secrets" / "linky")
    r = _check_cli(home)
    assert r.returncode == 1
    assert "linky" in _out(r)
    assert MARKER not in _out(r)


@needs_backend
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


@needs_backend
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


@needs_backend
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


@needs_backend
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


@needs_backend
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


@needs_backend
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


@needs_backend
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


@needs_backend
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
    monkeypatch.setattr(sys, "platform", "freebsd")  # neither linux nor darwin: the ACL half is not checked
    res = mod.check(home)
    assert res.code == 0
    assert capsys.readouterr().err.splitlines().count("secrets ACL not checked on this platform") == 1
    # L1(c): the stdout verdict itself says the ACL half was not checked
    assert mod.main(["--home", str(home)]) == 0
    assert "secrets check: pass (modes only; ACL not checked on this platform)" in capsys.readouterr().out.splitlines()

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


@needs_backend
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


@needs_backend
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


@needs_backend
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


@needs_backend
def test_r5_default_mapping_to_path_refused(tmp_path):
    _r5_refused(tmp_path, ["path"], ["path"], "PATH")


@needs_backend
def test_r5_default_mapping_with_dash_refused(tmp_path):
    _r5_refused(tmp_path, ["bash-env"], ["bash-env"], "BASH_ENV")


@needs_backend
@pytest.mark.parametrize(
    "var",
    ["LD_PRELOAD", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "BASH_FUNC_FOO", "NODE_OPTIONS", "HOME", "PS4"],
)
def test_r5_as_override_to_dangerous_var_refused(tmp_path, var):
    _r5_refused(tmp_path, ["--as", f"{var}=k", "k"], ["k"], var)


@needs_backend
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


@needs_backend
def test_f1_round3_default_mapping_via_python_prefix_message_names_as(tmp_path):
    _r5_refused(tmp_path, ["python-token"], ["python-token"], "PYTHON_TOKEN")
    r = _run(SECRET_ENV, ["--home", str(tmp_path / "home"), "python-token", "--", sys.executable, "-c", "pass"])
    assert "--as" in _out(r)


def test_f1_round3_prefix_and_name_sets_cover_the_additions():
    mod = _load_tool(SECRET_ENV, "secret_env_f1")
    for v in ("GIT_CONFIG_KEY_0", "CARGO_HOME", "NPM_CONFIG_PREFIX", "LC_MESSAGES"):
        assert mod.is_startup_var(v)
    assert not mod.is_startup_var("ANTHROPIC_API_KEY")


@needs_backend
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


@needs_backend
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
    os.chmod(reg, 0o600)  # read_secret now judges the mode too (Z1)

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


@needs_backend
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


@needs_backend
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


# ---------------------------------------------------------------- Z1-Z3 security-read fold (PR #4856)


def test_z1_read_secret_refuses_a_non_0600_regular_file_and_reads_a_0600_one(tmp_path):
    mod = _load_tool(SECRET_ENV, "secret_env_z1")
    f = tmp_path / "loose-secret"
    f.write_text(MARKER + "\n")
    os.chmod(f, 0o644)
    refused = named = clean = False
    try:
        mod.read_secret(f)
    except mod.SecretRefused as exc:
        refused = "mode" in str(exc)
        named = "loose-secret" in str(exc)
        clean = MARKER not in str(exc)
    assert refused
    assert named
    assert clean
    os.chmod(f, 0o600)
    assert mod.read_secret(f) == MARKER


@needs_backend
def test_z3a_dotfile_leftover_refuses_named_with_remediation(tmp_path):
    home = _clean_home(tmp_path)
    leftover = home / "secrets" / ".put-abc123"
    _write(leftover, MARKER + "\n")
    r = _check_cli(home)
    out = _out(r)
    assert r.returncode == 1
    assert ".put-abc123" in out
    assert "suspected leftover temporary file from an interrupted secret-put; remove it" in out
    assert "rm -- " + shlex.quote(str(leftover)) in out
    assert MARKER not in out


@needs_backend
def test_z3b_normal_put_leaves_no_dotfile_and_the_folder_still_passes(tmp_path):
    home = _clean_home(tmp_path, files=())
    r = _run(SECRET_PUT, ["--home", str(home), "k"], stdin=MARKER + "\n")
    assert r.returncode == 0
    assert MARKER not in _out(r)
    assert [p.name for p in (home / "secrets").iterdir() if p.name.startswith(".")] == []
    assert _check_cli(home).returncode == 0


@needs_backend
def test_z3c_secret_env_refuses_when_a_leftover_is_present(tmp_path):
    home = _marker_home(tmp_path)
    _write(home / "secrets" / ".put-zz9", MARKER + "\n")
    sentinel = tmp_path / "ran"
    r = _run(
        SECRET_ENV,
        ["--home", str(home), "anthropic-api-key.acct", "--", sys.executable, "-c",
         f"open({str(sentinel)!r}, 'w').close()"],
    )
    assert r.returncode == 1
    assert ".put-zz9" in _out(r)
    assert not sentinel.exists()
    assert MARKER not in _out(r)


# ---------------------------------------------------------------- L1-L5 colleague-read fold (PR #4856)
# Darwin behaviour is tested on Linux through a monkeypatched sys.platform and shims for `ls` and `chmod`
# that the DARWIN_LS / DARWIN_CHMOD constants are pointed at (the tools call /bin/ls and /bin/chmod by absolute path); the one real-macOS cell skips unless sys.platform == "darwin".

_LS_SHIM = """#!/bin/sh
[ -n "$SHIM_LOG" ] && echo "ls $*" >> "$SHIM_LOG"
if [ -n "$SHIM_LS_RC" ] && [ "$SHIM_LS_RC" != 0 ]; then exit "$SHIM_LS_RC"; fi
if [ -n "$SHIM_LS_GARBAGE" ]; then echo "$SHIM_LS_GARBAGE"; exit 0; fi
case "$1" in
  -ldeq) echo "$SHIM_DIR_MODE 2 user staff 64 Nov 16 16:25 x"; [ -n "$SHIM_DIR_AFTER" ] && printf '%s\\n' "$SHIM_DIR_AFTER";;
  -leq) echo "$SHIM_FILE_MODE 1 user staff 3 Nov 16 16:25 x"; [ -n "$SHIM_FILE_AFTER" ] && printf '%s\\n' "$SHIM_FILE_AFTER";;
  *) exit 9;;
esac
exit 0
"""
_CHMOD_SHIM = """#!/bin/sh
echo "chmod $*" >> "$SHIM_LOG"
exit "${SHIM_CHMOD_RC:-0}"
"""


def _darwin(tmp_path, monkeypatch, mod, dir_mode="drwx------", file_mode="-rw-------", dir_after="", file_after="", **extra):
    """Pretend to be macOS: platform patched and the tools' absolute-path constants (DARWIN_LS, DARWIN_CHMOD)
    pointed at shims, so PATH is not involved. `mod` is the loaded secrets_check or secret-put module."""
    shim = tmp_path / "darwinshim"
    shim.mkdir()
    for name, body in (("ls", _LS_SHIM), ("chmod", _CHMOD_SHIM)):
        (shim / name).write_text(body)
        os.chmod(shim / name, 0o755)
    for target in (mod, getattr(mod, "secrets_check", None)):  # secret-put re-checks through its own import
        if target is not None and hasattr(target, "DARWIN_LS"):
            monkeypatch.setattr(target, "DARWIN_LS", str(shim / "ls"))
    if hasattr(mod, "DARWIN_CHMOD"):
        monkeypatch.setattr(mod, "DARWIN_CHMOD", str(shim / "chmod"))
    log = tmp_path / "shim.log"
    monkeypatch.setenv("SHIM_LOG", str(log))
    monkeypatch.setenv("SHIM_DIR_MODE", dir_mode)
    monkeypatch.setenv("SHIM_FILE_MODE", file_mode)
    monkeypatch.setenv("SHIM_DIR_AFTER", dir_after)
    monkeypatch.setenv("SHIM_FILE_AFTER", file_after)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "platform", "darwin")
    return log


# What `ls -le` prints under a real ACL entry (Luria, macOS 27.0.1, with neutral names).
DIR_ENTRY = " 0: group:everyone allow list"
FILE_ENTRY = " 0: group:everyone allow read"


def test_l1a_darwin_plus_on_dir_refuses_and_names_chmod_N(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, dir_mode="drwx------+")
    assert mod.main(["--home", str(home)]) == 1
    out = capsys.readouterr().out
    assert "secrets: extended ACL" in out
    assert "chmod -N " + shlex.quote(str(home / "secrets")) in out


def test_l1a_darwin_plus_on_file_refuses_named_with_quoted_remediation(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path / "ho me's", files=("only-key",))
    _darwin(tmp_path, monkeypatch, mod, file_mode="-rw-------+")
    assert mod.main(["--home", str(home)]) == 1
    out = capsys.readouterr().out
    assert "only-key: extended ACL" in out
    lines = [shlex.split(ln.strip()) for ln in out.splitlines() if ln.strip().startswith("chmod ")]
    assert ["chmod", "-N", str(home / "secrets" / "only-key")] in lines


def test_l1a_darwin_clean_passes_without_the_not_checked_note(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path)
    log = _darwin(tmp_path, monkeypatch, mod)
    assert mod.main(["--home", str(home)]) == 0
    cap = capsys.readouterr()
    assert cap.out.splitlines() == ["secrets check: pass"]
    assert "not checked" not in cap.err
    calls = log.read_text().splitlines()
    assert any(c.startswith("ls -ldeq -- ") for c in calls)  # the directory
    assert any(c.startswith("ls -leq -- ") for c in calls)  # each file


@pytest.mark.parametrize("extra", [{"SHIM_LS_RC": "1"}, {"SHIM_LS_GARBAGE": "not a listing"}])
def test_l1a_darwin_ls_failing_or_unparseable_is_exit_2(tmp_path, monkeypatch, capsys, extra):
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, **extra)
    assert mod.main(["--home", str(home)]) == 2
    assert "cannot tell" in capsys.readouterr().out


def test_l1a_darwin_xattr_and_acl_entry_refuses_and_names_chmod_N_on_file(tmp_path, monkeypatch, capsys):
    """The defect: ls prints '@' (not '+') when an xattr exists, so the ACL is only in the entry lines."""
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, file_mode="-rw-------@", file_after=FILE_ENTRY)
    assert mod.main(["--home", str(home)]) == 1
    out = capsys.readouterr().out
    assert "alpha-key: extended ACL" in out
    assert "chmod -N " + shlex.quote(str(home / "secrets" / "alpha-key")) in out


def test_l1a_darwin_xattr_and_acl_entry_on_dir_refuses(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, dir_mode="drwx------@", dir_after=DIR_ENTRY)
    assert mod.main(["--home", str(home)]) == 1
    out = capsys.readouterr().out
    assert "secrets: extended ACL" in out
    assert "chmod -N " + shlex.quote(str(home / "secrets")) in out


def test_l1a_darwin_at_alone_passes(tmp_path, monkeypatch, capsys):
    """macOS adds com.apple.provenance to ordinary files: '@' with no entry line is clean."""
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, dir_mode="drwx------@", file_mode="-rw-------@")
    assert mod.main(["--home", str(home)]) == 0
    assert capsys.readouterr().out.splitlines() == ["secrets check: pass"]


@pytest.mark.parametrize(
    "kw",
    [
        {"dir_mode": "drwx------+", "dir_after": DIR_ENTRY},
        {"dir_mode": "drwx------+"},  # '+' with no entry line still refuses
        {"file_mode": "-rw-------+", "file_after": FILE_ENTRY},
        {"file_mode": "-rw-------+"},
        {"dir_after": DIR_ENTRY},  # no suffix, entry line present
        {"file_after": FILE_ENTRY},
    ],
)
def test_l1a_darwin_plus_or_entry_line_refuses(tmp_path, monkeypatch, capsys, kw):
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, **kw)
    assert mod.main(["--home", str(home)]) == 1
    assert "extended ACL" in capsys.readouterr().out


@pytest.mark.parametrize("suffix", ["%", ".", ":"])
def test_l1a_darwin_other_suffix_is_exit_2(tmp_path, monkeypatch, capsys, suffix):
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, file_mode="-rw-------" + suffix)
    assert mod.main(["--home", str(home)]) == 2
    assert "cannot tell" in capsys.readouterr().out


@pytest.mark.parametrize("junk", ["garbage", "total 8", " 0 no-colon-entry"])
def test_l1a_darwin_trailing_non_entry_line_is_exit_2_never_clean(tmp_path, monkeypatch, capsys, junk):
    """One clean line passes (cell below); the same line plus a later non-entry line must not."""
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod, file_after=junk)
    assert mod.main(["--home", str(home)]) == 2
    assert "cannot tell" in capsys.readouterr().out


def test_l1a_darwin_clean_listing_is_exactly_one_line_and_passes(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod)
    assert mod.main(["--home", str(home)]) == 0


def test_l1a_darwin_name_split_into_an_entry_shaped_line_never_passes(tmp_path, monkeypatch, capsys):
    """What ls WITHOUT -q could print for a file named "x<newline> 0: group:everyone allow read": the name's
    second half is an entry-shaped line. check() has no name rule, so the name reaches ls. The shim prints
    that split; the result is exit 1 (refuse), never 0. The real tool passes -q so ls prints '?' instead."""
    mod = _load()
    home = _clean_home(tmp_path, files=("alpha-key",))
    log = _darwin(tmp_path, monkeypatch, mod, file_after=FILE_ENTRY)
    assert mod.main(["--home", str(home)]) == 1
    assert any(c.startswith("ls -leq -- ") for c in log.read_text().splitlines())  # -q is in the flags


@needs_backend
def test_d3_other_dotfile_is_refused_as_an_unexpected_hidden_file(tmp_path):
    home = _clean_home(tmp_path)
    ds = home / "secrets" / ".DS_Store"
    _write(ds, "x\n")
    r = _check_cli(home)
    out = _out(r)
    assert r.returncode == 1
    assert ".DS_Store: unexpected hidden file in secrets/ (for example .DS_Store from Finder); remove it" in out
    assert "interrupted secret-put" not in out
    assert "rm -- " + shlex.quote(str(ds)) in out


def _put_in_process(mod, home, data=b"v\n", args=()):
    old = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(data))
    try:
        return mod.main(["--home", str(home), *args, "k"])
    finally:
        sys.stdin = old


def test_l1b_put_on_darwin_runs_chmod_N_on_the_folder_it_creates(tmp_path, monkeypatch):
    mod = _load_tool(SECRET_PUT, "secret_put_l1b")
    home = tmp_path / "home"
    home.mkdir()
    log = _darwin(tmp_path, monkeypatch, mod)
    assert _put_in_process(mod, home) == 0
    sec = home / "secrets"
    assert "chmod -N " + str(sec) in log.read_text().splitlines()
    assert (sec / "k").exists() and stat.S_IMODE(sec.stat().st_mode) == 0o700


def test_l1b_put_on_darwin_does_not_chmod_N_an_existing_folder(tmp_path, monkeypatch):
    mod = _load_tool(SECRET_PUT, "secret_put_l1b2")
    home = _clean_home(tmp_path, files=())
    log = _darwin(tmp_path, monkeypatch, mod)
    assert _put_in_process(mod, home) == 0
    assert "chmod -N" not in (log.read_text() if log.exists() else "")


def test_l1b_put_on_darwin_chmod_failure_removes_the_folder_exit_2(tmp_path, monkeypatch, capsys):
    mod = _load_tool(SECRET_PUT, "secret_put_l1b3")
    home = tmp_path / "home"
    home.mkdir()
    _darwin(tmp_path, monkeypatch, mod, SHIM_CHMOD_RC="1")
    assert _put_in_process(mod, home) == 2
    assert not (home / "secrets").exists()
    assert "Traceback" not in capsys.readouterr().err


# --- #4842 slice 2: the darwin tools are called by absolute path, never through PATH
_GNU_LS = """#!/bin/sh
echo "ls: invalid option -- 'e'" >&2
exit 2
"""
_GNU_CHMOD = """#!/bin/sh
echo "chmod: invalid option -- 'N'" >&2
exit 1
"""


def _gnu_first_on_path(tmp_path, monkeypatch):
    gnu = tmp_path / "gnufirst"
    gnu.mkdir()
    for name, body in (("ls", _GNU_LS), ("chmod", _GNU_CHMOD)):
        (gnu / name).write_text(body)
        os.chmod(gnu / name, 0o755)
    monkeypatch.setenv("PATH", str(gnu) + os.pathsep + os.environ["PATH"])


def test_l6a_darwin_check_ignores_a_gnu_ls_first_on_path(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path)
    log = _darwin(tmp_path, monkeypatch, mod)
    _gnu_first_on_path(tmp_path, monkeypatch)
    assert mod.main(["--home", str(home)]) == 0
    assert capsys.readouterr().out.splitlines() == ["secrets check: pass"]
    assert log.read_text()  # the DARWIN_LS shim, not the PATH one, answered


def test_l6b_darwin_put_ignores_a_gnu_chmod_first_on_path(tmp_path, monkeypatch):
    mod = _load_tool(SECRET_PUT, "secret_put_l6b")
    home = tmp_path / "home"
    home.mkdir()
    log = _darwin(tmp_path, monkeypatch, mod)
    _gnu_first_on_path(tmp_path, monkeypatch)
    assert _put_in_process(mod, home) == 0
    assert "chmod -N " + str(home / "secrets") in log.read_text().splitlines()
    assert (home / "secrets" / "k").exists()


def test_l6c_darwin_ls_missing_at_the_absolute_path_is_exit_2_with_no_path_fallback(tmp_path, monkeypatch, capsys):
    mod = _load()
    home = _clean_home(tmp_path)
    _darwin(tmp_path, monkeypatch, mod)
    # a working ls shim IS on PATH, so a fallback to PATH would pass
    monkeypatch.setenv("PATH", str(tmp_path / "darwinshim") + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(mod, "DARWIN_LS", str(tmp_path / "no-such-dir" / "ls"))
    assert mod.main(["--home", str(home)]) == 2
    assert "cannot tell" in capsys.readouterr().out


def test_l6d_darwin_chmod_missing_at_the_absolute_path_removes_the_folder_exit_2(tmp_path, monkeypatch, capsys):
    mod = _load_tool(SECRET_PUT, "secret_put_l6d")
    home = tmp_path / "home"
    home.mkdir()
    _darwin(tmp_path, monkeypatch, mod)
    monkeypatch.setenv("PATH", str(tmp_path / "darwinshim") + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(mod, "DARWIN_CHMOD", str(tmp_path / "no-such-dir" / "chmod"))
    assert _put_in_process(mod, home) == 2
    assert not (home / "secrets").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS ACL inheritance")
def test_l1_real_mac_inherited_acl_refused_and_fresh_put_is_clean(tmp_path):
    acl = "everyone allow list,search,file_inherit,directory_inherit"

    def acl_entries(path, is_dir):
        """ACL entry lines of `ls -le` / `ls -lde`: the mode suffix shows '@' instead of '+' when an
        xattr exists, so the entry lines are the only reliable reading."""
        out = subprocess.run(
            ["ls", "-lde" if is_dir else "-le", "--", str(path)], capture_output=True, text=True
        ).stdout
        return [ln for ln in out.splitlines()[1:] if re.match(r"^\s*\d+: \S", ln)]

    # Luria's measurement: a folder made under an inheritable-ACL home inherits the ACE and must be refused.
    home1 = tmp_path / "home1"
    home1.mkdir()
    subprocess.run(["chmod", "+a", acl, str(home1)], check=True)
    os.mkdir(home1 / "secrets", 0o700)
    _write(home1 / "secrets" / "k", "v\n")
    assert acl_entries(home1 / "secrets", True)
    assert _check_cli(home1).returncode == 1

    # A folder secret-put creates itself under the same kind of home must come out clean and pass.
    home2 = tmp_path / "home2"
    home2.mkdir()
    subprocess.run(["chmod", "+a", acl, str(home2)], check=True)
    r = _run(SECRET_PUT, ["--home", str(home2), "k"], stdin="v\n")
    assert r.returncode == 0
    assert not acl_entries(home2 / "secrets", True)
    assert not acl_entries(home2 / "secrets" / "k", False)
    assert _check_cli(home2).returncode == 0

    # Third half: the same inherited ACL AND an extended attribute on the folder and the file. `ls -l`
    # then prints '@' (not '+'), so a suffix-only check would pass; the entry lines must be seen.
    home3 = tmp_path / "home3"
    home3.mkdir()
    subprocess.run(["chmod", "+a", acl, str(home3)], check=True)
    os.mkdir(home3 / "secrets", 0o700)
    _write(home3 / "secrets" / "k", "v\n")
    for path in (home3 / "secrets", home3 / "secrets" / "k"):
        subprocess.run(["xattr", "-w", "user.test", "1", str(path)], check=True)
    assert acl_entries(home3 / "secrets", True)
    assert acl_entries(home3 / "secrets" / "k", False)
    assert _check_cli(home3).returncode == 1

    # A file named with a newline and an entry-shaped second half. check() has no name rule, so it reaches
    # ls; `ls -q` prints the newline as '?': one line, no injected entry. The file has no ACL, so the
    # EXPECTED outcome is exit 0 (a name cannot forge a refusal, and cannot hide an entry either).
    home4 = _clean_home(tmp_path / "home4", files=())
    _write(home4 / "secrets" / "x\n 0: group:everyone allow read", "v\n")
    assert _check_cli(home4).returncode == 0


def _child_cmp(var, expected):
    return ("import os;v=os.environ.get(%r);" % var) + "print('match' if v == %r else 'differ')" % expected


@needs_backend
def test_l2_raw_round_trips_trailing_newlines_byte_for_byte(tmp_path):
    home = _clean_home(tmp_path, files=())
    r = _run(SECRET_PUT, ["--home", str(home), "--raw", "keyfile"], stdin="abc\n\n")
    assert r.returncode == 0
    assert (home / "secrets" / "keyfile").read_bytes() == b"abc\n\n"
    r = _run(SECRET_ENV, ["--home", str(home), "--raw", "keyfile", "--",
                          sys.executable, "-c", _child_cmp("KEYFILE", "abc\n\n")])
    assert r.returncode == 0 and r.stdout.split() == ["match"]
    # without --raw on the way out, ONE newline is still removed
    r = _run(SECRET_ENV, ["--home", str(home), "keyfile", "--",
                          sys.executable, "-c", _child_cmp("KEYFILE", "abc\n")])
    assert r.stdout.split() == ["match"]


@needs_backend
def test_l2_without_raw_behaviour_is_unchanged(tmp_path):
    home = _clean_home(tmp_path, files=())
    assert _run(SECRET_PUT, ["--home", str(home), "plain"], stdin="abc\n\n").returncode == 0
    assert (home / "secrets" / "plain").read_bytes() == b"abc\n"
    r = _run(SECRET_ENV, ["--home", str(home), "plain", "--",
                          sys.executable, "-c", _child_cmp("PLAIN", "abc")])
    assert r.stdout.split() == ["match"]


@needs_backend
def test_l2_secret_env_raw_applies_to_every_name(tmp_path):
    home = _clean_home(tmp_path, files=())
    for n in ("one", "two"):
        assert _run(SECRET_PUT, ["--home", str(home), "--raw", n], stdin=n + "\n").returncode == 0
    child = ("import os;print('match' if (os.environ['ONE'], os.environ['TWO']) == ('one\\n', 'two\\n') "
             "else 'differ')")
    r = _run(SECRET_ENV, ["--home", str(home), "--raw", "one", "two", "--", sys.executable, "-c", child])
    assert r.returncode == 0 and r.stdout.split() == ["match"]


def test_l2_docstrings_say_plainly_that_a_key_file_needs_raw():
    for tool in (SECRET_PUT, SECRET_ENV):
        doc = _load_tool(tool, "doc_" + tool.name.replace("-", "_")).__doc__
        assert "--raw" in doc and "OpenSSH private key" in doc


@needs_backend
def test_l3_default_variable_name_is_validated(tmp_path):
    home = _clean_home(tmp_path, files=())
    _write(home / "secrets" / "9lives", "v\n")
    sentinel = tmp_path / "ran"
    r = _run(SECRET_ENV, ["--home", str(home), "9lives", "--", sys.executable, "-c",
                          f"open({str(sentinel)!r}, 'w').close()"])
    assert r.returncode == 1
    assert not sentinel.exists()
    assert "9LIVES" in _out(r) and "--as VAR=NAME" in _out(r)
    r = _run(SECRET_ENV, ["--home", str(home), "--as", "LIVES=9lives", "9lives", "--",
                          sys.executable, "-c", _child_cmp("LIVES", "v")])
    assert r.returncode == 0 and r.stdout.split() == ["match"]


@needs_acl
def test_l4_getfacl_not_found_names_the_package_not_an_exception_class(tmp_path):
    home = _clean_home(tmp_path)
    empty = tmp_path / "emptybin"
    empty.mkdir()
    r = _run(CHECK_PY, ["--home", str(home)], path=str(empty))
    assert r.returncode == 2
    assert "getfacl not found (install the acl package)" in _out(r)
    assert "FileNotFoundError" not in _out(r)
