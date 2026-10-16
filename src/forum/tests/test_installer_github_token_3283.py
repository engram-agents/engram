"""#3283 G7 -- install-forum-service.sh `--github-token-file`.

Contract-first: written before the implementation. Same harness as the #3312
login-map cells in test_installer.py (stub PATH, temp HOME, existing unit),
reused by import so the two cannot drift.

The installer renders ONE `EnvironmentFile=-PATH` line into [Service]; it never
reads or dereferences PATH. A unit that already carries a github-token
EnvironmentFile line is never replaced by one that lost it (refusal, exit 2,
nothing written, also under --dry-run).
"""

import hashlib

import pytest

from forum.tests.test_installer import _install, _noncomment, _rendered

_TOKEN_FILE = "/etc/engram-forum/github-token.env"
_LINE = f"EnvironmentFile=-{_TOKEN_FILE}"
_EXISTING = (
    "[Service]\nExecStart=/x/python -m forum.server --port 5002\n"
    f"{_LINE}\n"
)


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _service_section(text):
    after = text.split("[Service]", 1)[1]
    return after.split("[Install]", 1)[0]


class TestRendering:
    def test_dry_run_renders_exactly_one_line_inside_service(self, tmp_path):
        r, _, _ = _install(tmp_path, ["--github-token-file", _TOKEN_FILE])
        assert r.returncode == 0, r.stdout + r.stderr
        body = _rendered(r.stdout)
        lines = [ln for ln in _noncomment(body).splitlines() if ln.startswith("EnvironmentFile=")]
        assert lines == [_LINE]
        assert _LINE in _service_section(body)
        assert "{{" not in body

    def test_rendered_line_has_the_optional_dash_prefix(self, tmp_path):
        """R1: `-` makes a missing/unreadable file non-fatal to the unit (the gate
        refuses instead). The installer adds it; the PATH argument may not."""
        r, _, _ = _install(tmp_path, ["--github-token-file", _TOKEN_FILE])
        assert r.returncode == 0, r.stdout + r.stderr
        lines = [ln for ln in _noncomment(_rendered(r.stdout)).splitlines()
                 if ln.lstrip().startswith("EnvironmentFile")]
        assert lines == ["EnvironmentFile=-/etc/engram-forum/github-token.env"]

    def test_without_option_no_line_and_no_unsubstituted_placeholder(self, tmp_path):
        r, _, _ = _install(tmp_path, [])
        assert r.returncode == 0, r.stdout + r.stderr
        body = _rendered(r.stdout)
        assert "EnvironmentFile" not in _noncomment(body)
        assert "{{" not in body

    def test_path_is_not_dereferenced(self, tmp_path):
        # The fixture owns the path: absent by construction under tmp_path, never a
        # host path (the forum host really has /etc/engram-forum/github-token.env, #3580).
        absent = tmp_path / "nope" / "github-token.env"
        assert not absent.exists(), "fixture assumption"
        r, _, _ = _install(tmp_path, ["--github-token-file", str(absent)])
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"EnvironmentFile=-{absent}" in _noncomment(_rendered(r.stdout))
        assert not absent.exists() and not absent.parent.exists(), "installer touched the path"

    def test_composes_with_the_login_map_flag(self, tmp_path):
        r, _, _ = _install(tmp_path, ["--baton-login-map", "/etc/engram-forum/baton-login-map.json",
                                      "--github-token-file", _TOKEN_FILE])
        assert r.returncode == 0, r.stdout + r.stderr
        body = _noncomment(_rendered(r.stdout))
        assert _LINE in body and "--baton-login-map /etc/engram-forum/baton-login-map.json" in body


class TestCharset:
    @pytest.mark.parametrize("bad", [
        "relative/github-token.env", "-/etc/github-token.env", "/etc/a b/github-token.env", "/etc/a\"b/github-token.env",
        "/etc/a'b/github-token.env", "/etc/a\nExecStart=/bin/evil", "/etc/a$b", "/etc/a;b",
    ], ids=["relative", "leading-dash", "space", "dquote", "squote", "newline", "dollar", "semicolon"])
    def test_bad_path_refused(self, tmp_path, bad):
        r, _, log = _install(tmp_path, ["--github-token-file", bad])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "--github-token-file" in r.stderr
        assert "unknown arg" not in r.stderr, "the option must exist and refuse the value itself"
        assert not log.exists()


class TestNotSilentlyDropped:
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_reinstall_without_option_refuses_and_writes_nothing(self, tmp_path, dry_run):
        r, unit, log = _install(tmp_path, [], unit=_EXISTING, dry_run=dry_run)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "--github-token-file" in r.stderr and str(unit) in r.stderr
        assert unit.read_text() == _EXISTING
        assert not log.exists(), f"refusal must precede every action: {log.read_text()}"
        assert not (tmp_path / "forum").exists()

    def test_sha_identical_after_refusal(self, tmp_path):
        r0, unit, _ = _install(tmp_path, [], unit=_EXISTING)
        before = _sha(unit)
        r, unit, _ = _install(tmp_path, [], unit=_EXISTING)
        assert r.returncode == 2
        assert _sha(unit) == before

    def test_quoted_and_dash_prefixed_forms_also_guarded(self, tmp_path):
        existing = "[Service]\nEnvironmentFile=-/etc/engram-forum/github-token.env\n"
        r, unit, _ = _install(tmp_path, [], unit=existing)
        assert r.returncode == 2, r.stdout + r.stderr
        assert unit.read_text() == existing

    def test_reinstall_with_option_proceeds(self, tmp_path):
        r, _, _ = _install(tmp_path, ["--github-token-file", _TOKEN_FILE], unit=_EXISTING)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _LINE in _noncomment(_rendered(r.stdout))

    def test_comment_only_mention_does_not_trigger(self, tmp_path):
        existing = ("[Service]\nExecStart=/x\n"
                    "# EnvironmentFile=/etc/engram-forum/github-token.env\n"
                    "; EnvironmentFile=/etc/engram-forum/github-token.env\n")
        r, _, _ = _install(tmp_path, [], unit=existing)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_unrelated_environmentfile_does_not_trigger(self, tmp_path):
        existing = "[Service]\nEnvironmentFile=/etc/default/something-else\n"
        r, _, _ = _install(tmp_path, [], unit=existing)
        assert r.returncode == 0, r.stdout + r.stderr


class TestBasenameAndSpacedGuard:
    """Round-1 F1: the re-install guard keys on the name `github-token`, so the
    option must insist on it, and the guard must tolerate spaces around `=`."""

    @pytest.mark.parametrize("bad", [
        "/etc/engram-forum/gate-pat.env",
        "/etc/github-token/gate.env",
    ], ids=["other-basename", "dir-only"])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_basename_without_github_token_refused_and_nothing_written(self, tmp_path, bad, dry_run):
        r, unit, log = _install(tmp_path, ["--github-token-file", bad], dry_run=dry_run)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "unknown arg" not in r.stderr
        assert "github-token" in r.stderr and "--github-token-file" in r.stderr
        assert not unit.exists()
        assert not log.exists()
        assert not (tmp_path / "forum").exists()

    @pytest.mark.parametrize("line", [
        "EnvironmentFile = /x/github-token.env",
        "EnvironmentFile=\t/x/github-token.env",
        "  EnvironmentFile =-/x/github-token.env",
        'EnvironmentFile = "/x/github-token.env"',
    ], ids=["spaced", "tab", "dash-spaced", "quoted-spaced"])
    def test_spaced_existing_line_triggers_guard(self, tmp_path, line):
        existing = f"[Service]\nExecStart=/x\n{line}\n"
        r, unit, log = _install(tmp_path, [], unit=existing)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "--github-token-file" in r.stderr
        assert unit.read_text() == existing
        assert not log.exists()

    def test_control_spaced_unrelated_environmentfile_does_not_trigger(self, tmp_path):
        existing = "[Service]\nEnvironmentFile = /etc/default/something-else\n"
        r, _, _ = _install(tmp_path, [], unit=existing)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_control_spaced_commented_line_does_not_trigger(self, tmp_path):
        existing = "[Service]\n# EnvironmentFile = /x/github-token.env\n"
        r, _, _ = _install(tmp_path, [], unit=existing)
        assert r.returncode == 0, r.stdout + r.stderr
