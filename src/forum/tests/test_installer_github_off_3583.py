"""#3583 -- install-forum-service.sh `--github-integration off`.

Contract-first. `off` is the deliberate way to turn GitHub integration off on an
existing install: it drops ONLY the github-token EnvironmentFile line. The baton
login map keeps its own no-silent-drop guard (#3312), because the server's OFF
branch still loads the map for the id<->anchor binding check.

Same hermetic harness as test_installer_github_token_3283.py (dry-run, temp HOME,
planted unit, stubbed PATH), reused by import.
"""

import pytest

from forum.tests.test_installer import _install, _noncomment, _rendered

_TOKEN_FILE = "/etc/engram-forum/github-token.env"
_MAP = "/etc/engram-forum/baton-login-map.json"
_WITH_TOKEN = (
    "[Service]\nExecStart=/x/python -m forum.server --port 5002\n"
    f"EnvironmentFile=-{_TOKEN_FILE}\n"
)
_WITH_TOKEN_AND_MAP = (
    "[Service]\nExecStart=/x/python -m forum.server \\\n"
    f"    --port 5002 \\\n    --baton-login-map {_MAP}\n"
    f"EnvironmentFile=-{_TOKEN_FILE}\n"
)
_OFF = "Environment=FORUM_GITHUB_INTEGRATION=off"


def _env_lines(body):
    return [ln for ln in _noncomment(body).splitlines() if ln.startswith("EnvironmentFile")]


def test_off_drops_token_line_and_says_so(tmp_path):
    r, unit, _ = _install(tmp_path, ["--github-integration", "off"], unit=_WITH_TOKEN)
    assert r.returncode == 0, r.stdout + r.stderr
    body = _rendered(r.stdout)
    assert _OFF in _noncomment(body)
    assert _env_lines(body) == []
    dropped = [ln for ln in r.stdout.splitlines() if _TOKEN_FILE in ln and "will not carry" in ln]
    assert len(dropped) == 1, r.stdout
    assert "-" + _TOKEN_FILE not in dropped[0], "the leading dash must be stripped"
    # The line prints at guard time, before the write, so it must not claim the
    # write already happened.
    assert "dropping" not in dropped[0].lower(), dropped[0]


_TOKEN_FILE_2 = "/etc/engram-forum/second-github-token.env"
_WITH_TWO_TOKENS = (
    "[Service]\nExecStart=/x/python -m forum.server --port 5002\n"
    f"EnvironmentFile=-{_TOKEN_FILE}\n"
    f'EnvironmentFile="{_TOKEN_FILE_2}"\n'
)


def test_off_names_every_github_token_line_on_one_stdout_line(tmp_path):
    r, _, _ = _install(tmp_path, ["--github-integration", "off"], unit=_WITH_TWO_TOKENS)
    assert r.returncode == 0, r.stdout + r.stderr
    body = _rendered(r.stdout)
    assert _OFF in _noncomment(body)
    assert _env_lines(body) == []
    named = [ln for ln in r.stdout.splitlines() if "will not carry" in ln]
    assert len(named) == 1, r.stdout
    assert _TOKEN_FILE in named[0] and _TOKEN_FILE_2 in named[0], named[0]
    assert '"' not in named[0] and "-" + _TOKEN_FILE not in named[0], named[0]


def test_off_with_login_map_in_old_unit_still_refused(tmp_path):
    r, unit, log = _install(tmp_path, ["--github-integration", "off"], unit=_WITH_TOKEN_AND_MAP)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--baton-login-map" in r.stderr
    assert unit.read_text() == _WITH_TOKEN_AND_MAP
    assert not log.exists()


def test_off_with_login_map_resupplied_keeps_map(tmp_path):
    r, _, _ = _install(tmp_path, ["--github-integration", "off", "--baton-login-map", _MAP],
                       unit=_WITH_TOKEN_AND_MAP)
    assert r.returncode == 0, r.stdout + r.stderr
    body = _rendered(r.stdout)
    assert _OFF in _noncomment(body)
    assert f"--baton-login-map {_MAP}" in _noncomment(body)
    assert _env_lines(body) == []


def test_no_flag_still_refused_and_error_names_the_deliberate_way(tmp_path):
    r, unit, log = _install(tmp_path, [], unit=_WITH_TOKEN)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--github-token-file" in r.stderr
    assert "--github-integration off" in r.stderr
    assert unit.read_text() == _WITH_TOKEN
    assert not log.exists()


@pytest.mark.parametrize("unit_text", [None, _WITH_TOKEN], ids=["fresh", "with-token"])
def test_off_with_token_file_is_a_contradiction(tmp_path, unit_text):
    r, unit, log = _install(tmp_path, ["--github-integration", "off", "--github-token-file", _TOKEN_FILE],
                            unit=unit_text)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--github-integration" in r.stderr and "--github-token-file" in r.stderr
    assert "unknown arg" not in r.stderr
    assert not log.exists()
    if unit_text is not None:
        assert unit.read_text() == unit_text
    assert not (tmp_path / "forum").exists()


@pytest.mark.parametrize("flags", [
    ["--github-integration", "on"],
    ["--github-integration", "maybe"],
    ["--github-integration", ""],
    ["--github-integration"],
], ids=["on", "other", "empty", "no-value"])
def test_other_values_refused(tmp_path, flags):
    r, unit, log = _install(tmp_path, flags, unit=_WITH_TOKEN)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--github-integration" in r.stderr
    assert "unknown arg" not in r.stderr
    assert unit.read_text() == _WITH_TOKEN
    assert not log.exists()


def test_on_refusal_points_at_token_file(tmp_path):
    r, _, _ = _install(tmp_path, ["--github-integration", "on"])
    assert r.returncode == 2
    assert "--github-token-file" in r.stderr


def test_fresh_install_off_is_the_same_as_no_flag(tmp_path):
    r, _, _ = _install(tmp_path, ["--github-integration", "off"])
    assert r.returncode == 0, r.stdout + r.stderr
    body = _rendered(r.stdout)
    assert _OFF in _noncomment(body)
    assert _env_lines(body) == []
    assert not [ln for ln in r.stdout.splitlines() if "will not carry" in ln]


# ---------------------------------------------------------------------------
# Non-dry-run refusal cells (review F1 on PR #3592).
#
# Under --dry-run the installer never writes, so "nothing was written" cannot
# fail. Every refusal below runs WITHOUT --dry-run against the stubbed PATH
# (the #3312 pattern in test_installer.py): a refusal that fires too late leaves
# the service dir behind, rewrites the unit, or calls a write stub.
# ---------------------------------------------------------------------------


def _assert_refused_before_any_write(tmp_path, r, unit, log, unit_text):
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (tmp_path / "forum").exists(), "service dir was created before the refusal"
    assert not log.exists(), "a write stub was called:\n" + (log.read_text() if log.exists() else "")
    if unit_text is None:
        assert not unit.exists()
    else:
        assert unit.read_bytes() == unit_text.encode()


@pytest.mark.parametrize("unit_text", [None, _WITH_TOKEN], ids=["fresh", "with-token"])
def test_nondry_off_with_token_file_refused_before_any_write(tmp_path, unit_text):
    r, unit, log = _install(tmp_path, ["--github-integration", "off", "--github-token-file", _TOKEN_FILE],
                            unit=unit_text, dry_run=False)
    _assert_refused_before_any_write(tmp_path, r, unit, log, unit_text)
    assert "--github-integration" in r.stderr and "--github-token-file" in r.stderr


def test_nondry_off_with_login_map_in_old_unit_refused_before_any_write(tmp_path):
    r, unit, log = _install(tmp_path, ["--github-integration", "off"],
                            unit=_WITH_TOKEN_AND_MAP, dry_run=False)
    _assert_refused_before_any_write(tmp_path, r, unit, log, _WITH_TOKEN_AND_MAP)
    assert "--baton-login-map" in r.stderr


def test_nondry_no_flag_on_token_unit_refused_before_any_write(tmp_path):
    r, unit, log = _install(tmp_path, [], unit=_WITH_TOKEN, dry_run=False)
    _assert_refused_before_any_write(tmp_path, r, unit, log, _WITH_TOKEN)
    assert "--github-token-file" in r.stderr and "--github-integration off" in r.stderr


@pytest.mark.parametrize("flags", [
    ["--github-integration", "on"],
    ["--github-integration", "maybe"],
    ["--github-integration", ""],
    ["--github-integration"],
], ids=["on", "other", "empty", "no-value"])
def test_nondry_other_values_refused_before_any_write(tmp_path, flags):
    r, unit, log = _install(tmp_path, flags, unit=_WITH_TOKEN, dry_run=False)
    _assert_refused_before_any_write(tmp_path, r, unit, log, _WITH_TOKEN)
    assert "--github-integration" in r.stderr and "unknown arg" not in r.stderr
