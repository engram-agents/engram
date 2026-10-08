"""#3593 -- install-forum-service.sh: a drop-in-supplied token counts for the
re-install refusal.

Contract-first. The installer never edits drop-ins (`engram-forum.service.d/*.conf`);
it re-renders only the main unit. A token EnvironmentFile supplied by a drop-in stays
loaded, but the installer used to decide the rendered switch value (`on`/`off`) from
the main unit alone, so a re-run with no flag rendered `=off` over a live token.

Read scope: the user unit dir only (`$HOME/.config/systemd/user/<unit>.d/*.conf`),
live (non-comment) lines, plain file reads. No systemctl, no /etc.

Same hermetic harness as test_installer_github_off_3583.py, reused by import. The
drop-in is planted BEFORE _install runs (it creates the unit dir with exist_ok).
"""

import os

import pytest

from forum.tests.test_installer import _install, _noncomment, _rendered

_TOKEN = "/etc/engram-forum/github-token.env"
_NEW_TOKEN = "/x/github-token.env"
_MAIN = "[Service]\nExecStart=/x/python -m forum.server --port 5002\n"
_DROPIN_TOKEN = f"[Service]\nEnvironmentFile=-{_TOKEN}\n"
_ON = "Environment=FORUM_GITHUB_INTEGRATION=on"
_OFF = "Environment=FORUM_GITHUB_INTEGRATION=off"


def _plant(tmp_path, files):
    d = tmp_path / "home" / ".config" / "systemd" / "user" / "engram-forum.service.d"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


def _snapshot(d):
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


def _env_lines(body):
    return [ln for ln in _noncomment(body).splitlines() if ln.startswith("EnvironmentFile")]


def _assert_refused_before_any_write(tmp_path, r, unit, log, dropin_dir, before):
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (tmp_path / "forum").exists(), "service dir was created before the refusal"
    assert not log.exists(), "a write stub was called:\n" + (log.read_text() if log.exists() else "")
    assert unit.read_bytes() == _MAIN.encode()
    assert _snapshot(dropin_dir) == before


def _assert_passed_guards(r, log, dry_run):
    """rc-0 rows. Dry-run: the installer succeeds. Non-dry-run: the harness stubs
    rsync/cp/python3/systemctl to exit 97 (fail loud), so the run cannot finish --
    reaching the first stub (logged, rc 97, no ERROR line) proves every guard passed
    and nothing refused with rc 2."""
    if dry_run:
        assert r.returncode == 0, r.stdout + r.stderr
        return
    assert r.returncode == 97, r.stdout + r.stderr
    assert log.exists(), "the run never got past the guards to the first stub"
    assert "ERROR" not in r.stderr, r.stderr


# --- Row 1: drop-in token, no flag -> refused, names the drop-in -------------

@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_dropin_token_no_flag_refused_naming_dropin(tmp_path, dry_run):
    d = _plant(tmp_path, {"token.conf": _DROPIN_TOKEN})
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    assert r.returncode == 2, r.stdout + r.stderr
    assert str(d / "token.conf") in r.stderr, r.stderr
    assert "--github-token-file" in r.stderr and "--github-integration off" in r.stderr
    if not dry_run:
        _assert_refused_before_any_write(tmp_path, r, unit, log, d, before)


def test_dropin_and_main_token_both_named(tmp_path):
    main = _MAIN + f"EnvironmentFile=-{_TOKEN}\n"
    d = _plant(tmp_path, {"token.conf": _DROPIN_TOKEN})
    r, unit, log = _install(tmp_path, [], unit=main, dry_run=False)
    assert r.returncode == 2, r.stdout + r.stderr
    assert str(d / "token.conf") in r.stderr and str(unit) in r.stderr, r.stderr
    assert not (tmp_path / "forum").exists() and not log.exists()
    assert unit.read_text() == main


# --- Row 2: drop-in token + --github-token-file -> on, main gets its own line -

@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_dropin_token_with_token_file_renders_on(tmp_path, dry_run):
    d = _plant(tmp_path, {"token.conf": _DROPIN_TOKEN})
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, ["--github-token-file", _NEW_TOKEN], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    if dry_run:
        body = _noncomment(_rendered(r.stdout))
        assert _ON in body
        assert _env_lines(body) == [f"EnvironmentFile=-{_NEW_TOKEN}"]
    assert _snapshot(d) == before


# --- Row 3: drop-in token + off -> off, notice names the drop-in as not removed

@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_dropin_token_with_off_names_dropin_as_not_removed(tmp_path, dry_run):
    d = _plant(tmp_path, {"token.conf": _DROPIN_TOKEN})
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, ["--github-integration", "off"], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    if dry_run:
        body = _noncomment(_rendered(r.stdout))
        assert _OFF in body
        assert _env_lines(body) == []
    notice = [ln for ln in r.stdout.splitlines() if str(d / "token.conf") in ln]
    assert len(notice) == 1, r.stdout
    assert "not remove" in notice[0].lower(), notice[0]
    assert _snapshot(d) == before


# --- Row 4: commented drop-in token is not live ------------------------------

@pytest.mark.parametrize("mark", ["#", ";"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_commented_dropin_token_is_not_live(tmp_path, mark, dry_run):
    _plant(tmp_path, {"token.conf": f"[Service]\n{mark}EnvironmentFile=-{_TOKEN}\n"})
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    if dry_run:
        assert _OFF in _noncomment(_rendered(r.stdout))


# --- Row 5: drop-in sets the switch -> warn, not refused ---------------------

@pytest.mark.parametrize("line", [
    "Environment=FORUM_GITHUB_INTEGRATION=off",
    'Environment="FORUM_GITHUB_INTEGRATION=off"',
], ids=["plain", "quoted"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_dropin_switch_override_warns_once_and_is_not_refused(tmp_path, line, dry_run):
    d = _plant(tmp_path, {"switch.conf": f"[Service]\n{line}\n"})
    r, unit, log = _install(tmp_path, ["--github-token-file", _NEW_TOKEN], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    warn = [ln for ln in (r.stdout + r.stderr).splitlines() if str(d / "switch.conf") in ln]
    assert len(warn) == 1, r.stdout + r.stderr
    assert "FORUM_GITHUB_INTEGRATION" in warn[0] and "off" in warn[0], warn[0]
    assert "overrid" in warn[0].lower(), warn[0]
    if dry_run:
        assert _ON in _noncomment(_rendered(r.stdout)), "the installer still renders its own value"


# --- Row 4b: login map in a drop-in: no change, no refusal -------------------

def test_login_map_in_dropin_is_not_refused(tmp_path):
    _plant(tmp_path, {"map.conf": "[Service]\nEnvironment=ENGRAM_BATON_LOGIN_MAP=/etc/x/map.json\n"})
    r, _, _ = _install(tmp_path, [], unit=_MAIN)
    assert r.returncode == 0, r.stdout + r.stderr


# --- Row 6: other settings only -> rc 0, no warning (the live-host row) ------

_OTHER = "[Service]\nNice=5\nEnvironment=FORUM_PUBLIC_URL=https://x\n"


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_dropin_with_only_other_settings_is_silent(tmp_path, dry_run):
    d = _plant(tmp_path, {"override.conf": _OTHER})
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    assert "override.conf" not in r.stdout + r.stderr
    assert "warning" not in (r.stdout + r.stderr).lower()
    if dry_run:
        assert _OFF in _noncomment(_rendered(r.stdout))
    assert _snapshot(d) == before


def test_empty_dropin_dir_is_silent(tmp_path):
    _plant(tmp_path, {})
    r, _, _ = _install(tmp_path, [], unit=_MAIN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning" not in (r.stdout + r.stderr).lower()


# --- Round 2 (reviewer S3): token line shapes the scan must still catch ------

@pytest.mark.parametrize("body", [
    f"[Service]\n   \tEnvironmentFile=-{_TOKEN}\n",
    f"[Service]\nEnvironmentFile = -{_TOKEN}\n",
    f"[Service]\r\nEnvironmentFile=-{_TOKEN}\r\n",
], ids=["leading-whitespace", "spaces-around-equals", "crlf"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_dropin_token_line_shapes_are_refused(tmp_path, body, dry_run):
    d = _plant(tmp_path, {})
    (d / "token.conf").write_bytes(body.encode())
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    assert r.returncode == 2, r.stdout + r.stderr
    assert str(d / "token.conf") in r.stderr, r.stderr
    if not dry_run:
        _assert_refused_before_any_write(tmp_path, r, unit, log, d, before)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_non_conf_file_is_ignored(tmp_path, dry_run):
    """systemd reads *.conf only: a token or switch in token.conf.bak is not live."""
    d = _plant(tmp_path, {
        "token.conf.bak": _DROPIN_TOKEN,
        "switch.conf.bak": f"[Service]\n{_OFF}\n",
    })
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    assert ".bak" not in r.stdout + r.stderr
    assert "warning" not in (r.stdout + r.stderr).lower()
    assert _snapshot(d) == before


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_symlinked_conf_dropin_is_followed(tmp_path, dry_run):
    d = _plant(tmp_path, {})
    target = tmp_path / "shared-token.env"
    target.write_text(_DROPIN_TOKEN)
    (d / "token.conf").symlink_to(target)
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    assert r.returncode == 2, r.stdout + r.stderr
    assert str(d / "token.conf") in r.stderr, r.stderr
    if not dry_run:
        assert not (tmp_path / "forum").exists() and not log.exists()
        assert unit.read_bytes() == _MAIN.encode()


# --- Round 2 (reviewer S1): switch override shapes ---------------------------

@pytest.mark.parametrize("line,value", [
    ("Environment = FORUM_GITHUB_INTEGRATION=off", "off"),
    ("Environment=FOO=1 FORUM_GITHUB_INTEGRATION=off", "off"),
    ('Environment="FOO=1" "FORUM_GITHUB_INTEGRATION=off"', "off"),
    ("  Environment =  FOO=1   FORUM_GITHUB_INTEGRATION=off  ", "off"),
    ("Environment=FORUM_GITHUB_INTEGRATION=off FOO=1", "off"),
    ("Environment=FORUM_GITHUB_INTEGRATION=off FORUM_GITHUB_INTEGRATION=on", "on"),
    ("Environment=FORUM_GITHUB_INTEGRATION=off\r", "off"),
], ids=["spaces-around-equals", "multi-plain", "multi-quoted", "mixed-spacing",
        "switch-first", "last-in-line-wins", "crlf"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_dropin_switch_override_shapes_warn(tmp_path, line, value, dry_run):
    d = _plant(tmp_path, {})
    (d / "switch.conf").write_bytes(f"[Service]\n{line}\n".encode())
    r, unit, log = _install(tmp_path, ["--github-token-file", _NEW_TOKEN], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    warn = [ln for ln in (r.stdout + r.stderr).splitlines() if str(d / "switch.conf") in ln]
    assert len(warn) == 1, r.stdout + r.stderr
    assert f"sets FORUM_GITHUB_INTEGRATION={value}, which" in warn[0], warn[0]


def test_dropin_switch_last_assignment_across_lines_wins(tmp_path):
    d = _plant(tmp_path, {"switch.conf": f"[Service]\n{_OFF}\nEnvironment = FOO=1 FORUM_GITHUB_INTEGRATION=on\n"})
    r, _, _ = _install(tmp_path, ["--github-token-file", _NEW_TOKEN], unit=_MAIN)
    warn = [ln for ln in (r.stdout + r.stderr).splitlines() if str(d / "switch.conf") in ln]
    assert len(warn) == 1 and "sets FORUM_GITHUB_INTEGRATION=on, which" in warn[0], warn


def test_dropin_other_environment_words_do_not_warn(tmp_path):
    _plant(tmp_path, {"o.conf": "[Service]\nEnvironment = FOO=1 BAR=2\nEnvironment=\"X_FORUM_GITHUB_INTEGRATION=off\"\n"})
    r, _, _ = _install(tmp_path, [], unit=_MAIN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning" not in (r.stdout + r.stderr).lower()


# --- Round 2 (reviewer S2): unreadable drop-ins refuse loud ------------------

_skip_root = pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")


@_skip_root
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_unreadable_dropin_file_refuses_loud(tmp_path, dry_run):
    d = _plant(tmp_path, {"token.conf": _DROPIN_TOKEN})
    f = d / "token.conf"
    f.chmod(0)
    try:
        r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
        assert r.returncode == 2, r.stdout + r.stderr
        assert f"ERROR: cannot read drop-in {f}; nothing was written" in r.stderr, r.stderr
        assert "Permission denied" not in r.stderr, "raw shell error leaked: " + r.stderr
        assert not (tmp_path / "forum").exists() and not log.exists()
        assert unit.read_bytes() == _MAIN.encode()
    finally:
        f.chmod(0o644)


@_skip_root
def test_unreadable_dropin_file_refuses_even_with_off(tmp_path):
    d = _plant(tmp_path, {"x.conf": "[Service]\nNice=5\n"})
    f = d / "x.conf"
    f.chmod(0)
    try:
        r, unit, log = _install(tmp_path, ["--github-integration", "off"], unit=_MAIN, dry_run=False)
        assert r.returncode == 2, r.stdout + r.stderr
        assert f"cannot read drop-in {f}; nothing was written" in r.stderr, r.stderr
        assert not log.exists()
    finally:
        f.chmod(0o644)


@_skip_root
@pytest.mark.parametrize("mode", [0, 0o444], ids=["mode-000", "no-search"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_unreadable_dropin_dir_refuses_loud(tmp_path, mode, dry_run):
    d = _plant(tmp_path, {"token.conf": _DROPIN_TOKEN})
    d.chmod(mode)
    try:
        r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
        assert r.returncode == 2, r.stdout + r.stderr
        assert f"ERROR: cannot read drop-in directory {d}; nothing was written" in r.stderr, r.stderr
        assert not (tmp_path / "forum").exists() and not log.exists()
        assert unit.read_bytes() == _MAIN.encode()
    finally:
        d.chmod(0o755)
