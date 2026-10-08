"""Follow-up to #3593 (the drop-in guard): pins and two small behaviours found in review.

Contract-first. Same hermetic harness as test_installer_dropin_guard_3593.py (imported).

Pins of behaviour that was already right:
  * two token-bearing drop-ins -> the refusal names BOTH files (non-dry-run arm);
  * the override warning prints the RENDERED value, `on` with --github-token-file and
    `off` without it;
  * a glob word in the same Environment= line (`Environment=* FORUM_GITHUB_INTEGRATION=off`)
    does not hide the switch.

Behaviour added here:
  * an EMPTY `FORUM_GITHUB_INTEGRATION=` in a drop-in gets its own warning: the server
    treats an empty value as unset (ON), so it overrides an installer-rendered `off`;
  * a token supplied directly as a live `Environment=GH_TOKEN=...` word in a drop-in
    counts as "token present" for the re-install refusal (and is named as not removed
    under `--github-integration off`); the token value is never echoed;
  * the override warning prints a value quoted only around itself without stray quotes.
"""

import pytest

from forum.tests.test_installer import _install, _noncomment, _rendered
from forum.tests.test_installer_dropin_guard_3593 import (
    _MAIN, _NEW_TOKEN, _OFF, _assert_passed_guards, _assert_refused_before_any_write,
    _plant, _snapshot, _TOKEN,
)

_TOKEN_CONF = f"[Service]\nEnvironmentFile=-{_TOKEN}\n"
_SECRET = "ghp_SyntheticSecretValue0123"


def _warn_lines(r, path):
    return [ln for ln in (r.stdout + r.stderr).splitlines() if str(path) in ln]


# --- Item 3: two token-bearing drop-ins -> both named ------------------------

def test_two_token_dropins_are_both_named_in_the_refusal(tmp_path):
    d = _plant(tmp_path, {"a.conf": _TOKEN_CONF, "b.conf": _TOKEN_CONF})
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=False)
    _assert_refused_before_any_write(tmp_path, r, unit, log, d, before)
    assert str(d / "a.conf") in r.stderr and str(d / "b.conf") in r.stderr, r.stderr


def test_two_token_dropins_are_both_named_under_off(tmp_path):
    d = _plant(tmp_path, {"a.conf": _TOKEN_CONF, "b.conf": _TOKEN_CONF})
    r, _, log = _install(tmp_path, ["--github-integration", "off"], unit=_MAIN, dry_run=False)
    _assert_passed_guards(r, log, False)
    assert str(d / "a.conf") in r.stdout and str(d / "b.conf") in r.stdout, r.stdout


# --- Item 4: the warning carries the RENDERED value --------------------------

@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
@pytest.mark.parametrize("flags,rendered", [
    (["--github-token-file", _NEW_TOKEN], "on"),
    ([], "off"),
], ids=["token-flag-renders-on", "no-flag-renders-off"])
def test_override_warning_names_the_rendered_value(tmp_path, flags, rendered, dry_run):
    d = _plant(tmp_path, {"switch.conf": f"[Service]\n{_OFF}\n"})
    r, _, log = _install(tmp_path, flags, unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    warn = _warn_lines(r, d / "switch.conf")
    assert len(warn) == 1, r.stdout + r.stderr
    assert f"renders ({rendered})" in warn[0], warn[0]


# --- Item 5: a glob word does not hide the switch ----------------------------

@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_glob_word_before_the_switch_still_warns(tmp_path, dry_run):
    d = _plant(tmp_path, {"switch.conf": "[Service]\nEnvironment=* FORUM_GITHUB_INTEGRATION=off\n"})
    r, _, log = _install(tmp_path, ["--github-token-file", _NEW_TOKEN], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    warn = _warn_lines(r, d / "switch.conf")
    assert len(warn) == 1 and "sets FORUM_GITHUB_INTEGRATION=off, which" in warn[0], warn


# --- Item 6: an empty value is treated as unset (ON) by the server -----------

_EMPTY_WARNING = ("sets FORUM_GITHUB_INTEGRATION to an empty value, which the server "
                  "treats as unset (ON)")


@pytest.mark.parametrize("line", [
    "Environment=FORUM_GITHUB_INTEGRATION=",
    'Environment=FORUM_GITHUB_INTEGRATION=""',
    'Environment="FORUM_GITHUB_INTEGRATION="',
    "Environment = FOO=1 FORUM_GITHUB_INTEGRATION=",
    "Environment=FORUM_GITHUB_INTEGRATION=\r",
    "Environment=FORUM_GITHUB_INTEGRATION=off FORUM_GITHUB_INTEGRATION=",
], ids=["plain", "empty-quotes", "quoted-word", "multi", "crlf", "empty-last-wins"])
@pytest.mark.parametrize("flags,rendered", [
    ([], "off"),
    (["--github-token-file", _NEW_TOKEN], "on"),
], ids=["renders-off", "renders-on"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_empty_switch_value_warns_that_it_means_on(tmp_path, line, flags, rendered, dry_run):
    d = _plant(tmp_path, {})
    (d / "switch.conf").write_bytes(f"[Service]\n{line}\n".encode())
    before = _snapshot(d)
    r, _, log = _install(tmp_path, flags, unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    warn = _warn_lines(r, d / "switch.conf")
    assert len(warn) == 1, r.stdout + r.stderr
    assert _EMPTY_WARNING in warn[0], warn[0]
    assert f"it overrides the value this installer renders ({rendered})" in warn[0], warn[0]
    assert _snapshot(d) == before


def test_empty_then_nonempty_assignment_warns_with_the_last_value(tmp_path):
    d = _plant(tmp_path, {"switch.conf": "[Service]\nEnvironment=FORUM_GITHUB_INTEGRATION=\n"
                                         "Environment=FORUM_GITHUB_INTEGRATION=off\n"})
    r, _, _ = _install(tmp_path, [], unit=_MAIN)
    warn = _warn_lines(r, d / "switch.conf")
    assert len(warn) == 1 and "sets FORUM_GITHUB_INTEGRATION=off, which" in warn[0], warn


def test_other_empty_assignments_do_not_warn(tmp_path):
    _plant(tmp_path, {"o.conf": "[Service]\nEnvironment=FOO= BAR=2\n"
                                'Environment="X_FORUM_GITHUB_INTEGRATION="\n'})
    r, _, _ = _install(tmp_path, [], unit=_MAIN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning" not in (r.stdout + r.stderr).lower()


# --- Item 7: a live GH_TOKEN word in a drop-in counts as a token -------------

@pytest.mark.parametrize("line", [
    f"Environment=GH_TOKEN={_SECRET}",
    f'Environment="GH_TOKEN={_SECRET}"',
    f"Environment=FOO=1 GH_TOKEN={_SECRET} BAR=2",
    f'Environment="FOO=1" "GH_TOKEN={_SECRET}"',
    f"Environment = GH_TOKEN={_SECRET}",
    f"  Environment =  FOO=1   GH_TOKEN={_SECRET}  ",
    f"Environment=GH_TOKEN={_SECRET}\r",
], ids=["plain", "quoted", "multi", "multi-quoted", "spaces-around-equals", "mixed-spacing", "crlf"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_gh_token_word_in_a_dropin_is_refused_without_a_flag(tmp_path, line, dry_run):
    d = _plant(tmp_path, {})
    (d / "tok.conf").write_bytes(f"[Service]\n{line}\n".encode())
    before = _snapshot(d)
    r, unit, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    assert r.returncode == 2, r.stdout + r.stderr
    assert str(d / "tok.conf") in r.stderr, r.stderr
    assert "--github-token-file" in r.stderr and "--github-integration off" in r.stderr
    assert _SECRET not in r.stdout + r.stderr, "the token value was echoed"
    if not dry_run:
        _assert_refused_before_any_write(tmp_path, r, unit, log, d, before)


def test_gh_token_word_and_token_envfile_in_one_dropin_is_named_once(tmp_path):
    d = _plant(tmp_path, {"tok.conf": f"[Service]\nEnvironmentFile=-{_TOKEN}\n"
                                      f"Environment=GH_TOKEN={_SECRET}\n"})
    r, _, _ = _install(tmp_path, [], unit=_MAIN, dry_run=False)
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stderr.count(str(d / "tok.conf")) == 1, r.stderr


def test_gh_token_word_in_one_dropin_and_envfile_in_another_names_both(tmp_path):
    d = _plant(tmp_path, {"a.conf": f"[Service]\nEnvironment=GH_TOKEN={_SECRET}\n",
                          "b.conf": _TOKEN_CONF})
    r, _, _ = _install(tmp_path, [], unit=_MAIN, dry_run=False)
    assert r.returncode == 2, r.stdout + r.stderr
    assert str(d / "a.conf") in r.stderr and str(d / "b.conf") in r.stderr, r.stderr


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_gh_token_word_with_token_file_passes_the_guard(tmp_path, dry_run):
    d = _plant(tmp_path, {"tok.conf": f"[Service]\nEnvironment=GH_TOKEN={_SECRET}\n"})
    before = _snapshot(d)
    r, _, log = _install(tmp_path, ["--github-token-file", _NEW_TOKEN], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    assert _snapshot(d) == before


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_gh_token_word_with_off_is_named_as_not_removed(tmp_path, dry_run):
    d = _plant(tmp_path, {"tok.conf": f"[Service]\nEnvironment=GH_TOKEN={_SECRET}\n"})
    before = _snapshot(d)
    r, _, log = _install(tmp_path, ["--github-integration", "off"], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    if dry_run:
        assert _OFF in _noncomment(_rendered(r.stdout))
    notice = [ln for ln in r.stdout.splitlines() if str(d / "tok.conf") in ln]
    assert len(notice) == 1, r.stdout
    assert "not remove" in notice[0].lower(), notice[0]
    assert _SECRET not in r.stdout + r.stderr, "the token value was echoed"
    assert _snapshot(d) == before


@pytest.mark.parametrize("line", [
    f"Environment=X_GH_TOKEN={_SECRET}",
    f"Environment=GH_TOKEN_FILE=/etc/x/{_SECRET}",
    f'Environment="X_GH_TOKEN={_SECRET}" GH_TOKEN_FILE=/y',
    f"#Environment=GH_TOKEN={_SECRET}",
    f";Environment=GH_TOKEN={_SECRET}",
    "Environment=GH_TOKEN=",
    f"Environment=NOT_GH_TOKEN={_SECRET}",
    f"ExecStartPre=/bin/true GH_TOKEN={_SECRET}",
], ids=["x-prefix", "token-file", "mixed-lookalikes", "hash-comment", "semicolon-comment",
        "empty-value", "other-name-suffix", "not-an-environment-line"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_lookalike_or_inert_gh_token_words_are_not_a_token(tmp_path, line, dry_run):
    d = _plant(tmp_path, {})
    (d / "o.conf").write_bytes(f"[Service]\n{line}\n".encode())
    r, _, log = _install(tmp_path, [], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    assert "o.conf" not in r.stdout + r.stderr
    if dry_run:
        assert _OFF in _noncomment(_rendered(r.stdout))


# --- Item 8: a value quoted only around itself prints clean ------------------

@pytest.mark.parametrize("line", [
    'Environment=FORUM_GITHUB_INTEGRATION="off"',
    "Environment=FORUM_GITHUB_INTEGRATION='off'",
    'Environment="FORUM_GITHUB_INTEGRATION=off"',
    'Environment=FOO=1 FORUM_GITHUB_INTEGRATION="off"',
], ids=["value-double", "value-single", "word-double", "multi-value-double"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry", "nondry"])
def test_value_quoted_around_itself_prints_without_stray_quotes(tmp_path, line, dry_run):
    d = _plant(tmp_path, {"switch.conf": f"[Service]\n{line}\n"})
    r, _, log = _install(tmp_path, ["--github-token-file", _NEW_TOKEN], unit=_MAIN, dry_run=dry_run)
    _assert_passed_guards(r, log, dry_run)
    warn = _warn_lines(r, d / "switch.conf")
    assert len(warn) == 1, r.stdout + r.stderr
    assert "sets FORUM_GITHUB_INTEGRATION=off, which" in warn[0], warn[0]
    # the only quote characters allowed on the line come from nowhere: none at all.
    assert '"' not in warn[0] and "'" not in warn[0], warn[0]
