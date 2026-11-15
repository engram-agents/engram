"""Tests for forum/deploy/install-forum-service.sh (part of #868 slice B).

Part 2 installer promotion tests:
- --dry-run flag: action plan lines printed, no systemd/filesystem side-effects.
- Template rendering: rendered unit contains substituted service-dir + port,
  carries UMask=0022 (#4499) and the ONE-INSTANCE invariant comment.
- Foreign-dir guard: abort if --service-dir exists, is non-empty, and has no app/.
- DB preservation: forum.db not overwritten.

These tests invoke the installer script via subprocess (bash --dry-run) and
inspect stdout/stderr. No systemd calls are made in --dry-run mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate installer and template paths (relative to worktree).
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent.parent.parent  # src/forum/tests/ → src/forum/ → src/ → repo root
_INSTALLER = _REPO_ROOT / "src" / "forum" / "deploy" / "install-forum-service.sh"
_TEMPLATE = _REPO_ROOT / "src" / "forum" / "deploy" / "engram-forum.service.template"


def _run_installer(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run the installer shell script and return the CompletedProcess."""
    cmd = ["bash", str(_INSTALLER)] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """#4393: the installer's --dry-run reads the CALLER's real unit under
    $HOME/.config/systemd/user. Point HOME and XDG_CONFIG_HOME at a per-test
    scratch dir (outside tmp_path, which cells inspect) so a seat's real unit
    never reaches these cells. Cells that pass their own env=HOME still win."""
    h = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    return h


# ---------------------------------------------------------------------------
# --dry-run: action plan lines printed, no filesystem mutations.
# ---------------------------------------------------------------------------

class TestDryRun:
    """--dry-run produces action plan output and causes no side-effects."""

    def test_dry_run_exits_0(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        assert result.returncode == 0, (
            f"--dry-run should exit 0, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_dry_run_prints_dry_run_mode_banner(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "dry-run" in combined.lower(), (
            f"Expected dry-run banner in output, got:\n{combined}"
        )

    def test_dry_run_mentions_snapshot_action(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "forum/" in combined or "Snapshot" in combined or "rsync" in combined, (
            f"Expected snapshot/copy action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_mentions_venv_action(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "venv" in combined.lower(), (
            f"Expected venv action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_mentions_unit_rendering(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        # Should mention the unit file or systemd rendering
        assert "unit" in combined.lower() or "systemd" in combined.lower() or "service" in combined.lower(), (
            f"Expected unit rendering action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_mentions_systemctl_actions(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        # systemctl daemon-reload + enable should be mentioned
        assert "daemon-reload" in combined or "systemctl" in combined, (
            f"Expected systemctl action in dry-run output, got:\n{combined}"
        )

    def test_dry_run_no_filesystem_creation(self, tmp_path):
        """--dry-run must not create any files/dirs inside --service-dir."""
        service_dir = tmp_path / "forum_dry_test"
        _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--group", "agents", "--dry-run",
        ])
        # Service dir should not be created (umask/mkdir are dry-run printed)
        assert not service_dir.exists(), (
            f"--dry-run must not create {service_dir}; it was created anyway."
        )

    def test_dry_run_custom_port_in_output(self, tmp_path):
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(tmp_path / "forum"),
            "--port", "5099",
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "5099" in combined, (
            f"Expected port 5099 mentioned in dry-run output, got:\n{combined}"
        )


# ---------------------------------------------------------------------------
# Template rendering: rendered unit has correct substitutions + invariants.
# ---------------------------------------------------------------------------

class TestTemplateRendering:
    """Render the template manually and assert key properties."""

    def _render_template(self, service_dir: str, port: str = "5002") -> str:
        """Render the service template the same way the installer does."""
        template_text = _TEMPLATE.read_text(encoding="utf-8")
        # Strip >>>TEMPLATE-ONLY ... <<<TEMPLATE-ONLY block.
        lines = template_text.splitlines()
        filtered = []
        in_block = False
        for line in lines:
            if ">>>TEMPLATE-ONLY" in line:
                in_block = True
                continue
            if "<<<TEMPLATE-ONLY" in line:
                in_block = False
                continue
            if not in_block:
                filtered.append(line)
        rendered = "\n".join(filtered)
        # Substitute {{FORUM_HOME}} and port.
        rendered = rendered.replace("{{HOST}}", "127.0.0.1")
        rendered = rendered.replace("{{FORUM_HOME}}", service_dir)
        rendered = rendered.replace("5002", port)
        # No --baton-login-map given: the installer renders the placeholder to nothing.
        rendered = rendered.replace("{{BATON_LOGIN_MAP_ARGS}}", "")
        return rendered

    def test_rendered_unit_contains_service_dir(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert service_dir in rendered, (
            f"Rendered unit does not contain --service-dir path {service_dir!r}:\n{rendered}"
        )

    def test_rendered_unit_contains_port(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir, port="5099")
        assert "5099" in rendered, (
            f"Rendered unit does not contain port 5099:\n{rendered}"
        )

    def test_rendered_unit_carries_umask_0022(self, tmp_path):
        # #4499: no group write. A LINE match: the substring "UMask=002" is also in "UMask=0022".
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        lines = [ln.strip() for ln in rendered.splitlines()]
        assert "UMask=0022" in lines and "UMask=002" not in lines, (
            f"Rendered unit must carry UMask=0022 (no group write, #4499):\n{rendered}"
        )

    def test_rendered_unit_retains_one_instance_comment(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        # The template carries an INVARIANT comment about one instance at a time.
        assert "INVARIANT" in rendered or "one" in rendered.lower(), (
            f"Rendered unit must retain the ONE-INSTANCE invariant comment:\n{rendered}"
        )

    def test_rendered_unit_has_no_template_only_block(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert "TEMPLATE-ONLY" not in rendered, (
            f"Rendered unit must not contain the >>>TEMPLATE-ONLY block:\n{rendered}"
        )

    def test_rendered_unit_has_no_unsubstituted_placeholders(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert "{{" not in rendered, (
            f"Rendered unit has unsubstituted placeholders:\n{rendered}"
        )

    def test_rendered_unit_contains_venv_python(self, tmp_path):
        service_dir = str(tmp_path / "custom_forum")
        rendered = self._render_template(service_dir)
        assert ".venv/bin/python" in rendered, (
            f"Rendered unit must reference the venv Python:\n{rendered}"
        )


# ---------------------------------------------------------------------------
# Foreign-dir guard.
# ---------------------------------------------------------------------------

class TestForeignDirGuard:
    """Abort if --service-dir exists, is non-empty, and has no app/ subdir."""

    def test_empty_service_dir_ok(self, tmp_path):
        service_dir = tmp_path / "empty_forum"
        service_dir.mkdir()
        # Empty dir is fine — installer should proceed (and exit 0 in --dry-run).
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--group", "agents", "--dry-run",
        ])
        assert result.returncode == 0, (
            f"Empty service-dir should be accepted, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_service_dir_with_app_subdir_ok(self, tmp_path):
        service_dir = tmp_path / "forum_with_app"
        service_dir.mkdir()
        (service_dir / "app").mkdir()
        # Has app/ → looks like ours → should proceed.
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--group", "agents", "--dry-run",
        ])
        assert result.returncode == 0, (
            f"service-dir with app/ should be accepted, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_foreign_service_dir_aborts(self, tmp_path):
        service_dir = tmp_path / "foreign"
        service_dir.mkdir()
        # Non-empty with no app/ → foreign.
        (service_dir / "some_foreign_file.txt").write_text("foreign content")
        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--group", "agents", "--dry-run",
        ])
        assert result.returncode != 0, (
            f"Foreign service-dir should cause non-zero exit, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "foreign" in combined.lower() or "ERROR" in combined, (
            f"Expected foreign-dir error message:\n{combined}"
        )


# ---------------------------------------------------------------------------
# DB preservation: forum.db MUST survive an installer re-run unchanged.
# ---------------------------------------------------------------------------

class TestDBPreservation:
    """A pre-existing forum.db must survive an installer re-run unchanged.

    Structural invariant: the installer's copy step targets $SERVICE_DIR/app/forum/
    (code snapshot only). forum.db lives at $SERVICE_DIR/forum.db — a sibling of
    app/, not inside it. These paths never overlap, so rsync/cp cannot reach the DB.

    Two test forms, both required to prove the invariant:
      1. --dry-run integration: pre-create service-dir + forum.db + app/ marker,
         run the installer, assert DB is byte-identical afterward.
      2. Unit-level path test: show that the rsync/cp target path ($SERVICE_DIR/app/forum/)
         is strictly disjoint from $SERVICE_DIR/forum.db — i.e., the DB path is never
         a prefix-match of the copy destination.
    """

    def test_dry_run_db_preserved(self, tmp_path):
        """DB byte content is unchanged after a --dry-run installer pass."""
        service_dir = tmp_path / "forum"
        service_dir.mkdir()
        # Pre-create the app/ marker (foreign-dir guard passes).
        (service_dir / "app").mkdir()
        # Pre-create a forum.db with known content.
        db_file = service_dir / "forum.db"
        sentinel = b"SENTINEL_DB_CONTENT_DO_NOT_TOUCH"
        db_file.write_bytes(sentinel)

        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--group", "agents", "--dry-run",
        ])
        assert result.returncode == 0, (
            f"--dry-run should exit 0 even with pre-existing forum.db:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # The DB must still exist and be byte-identical.
        assert db_file.exists(), (
            "forum.db was deleted by --dry-run installer pass!"
        )
        assert db_file.read_bytes() == sentinel, (
            f"forum.db content was changed by --dry-run installer pass!\n"
            f"Expected: {sentinel!r}\nGot: {db_file.read_bytes()!r}"
        )

    def test_db_path_disjoint_from_copy_target(self, tmp_path):
        """Structural unit-level check: rsync/cp target path cannot address forum.db.

        The copy step writes to $SERVICE_DIR/app/forum/ (code snapshot).
        forum.db lives at $SERVICE_DIR/forum.db — one level up from app/.
        These are structurally disjoint; no traversal from app/forum/ reaches ../forum.db.
        """
        service_dir = tmp_path / "forum"
        db_path = service_dir / "forum.db"
        copy_target = service_dir / "app" / "forum"

        # The DB path must NOT be relative to (inside) the copy target.
        try:
            db_path.relative_to(copy_target)
            is_inside = True
        except ValueError:
            is_inside = False

        assert not is_inside, (
            f"forum.db at {db_path} is INSIDE the copy target {copy_target} — "
            "the installer could overwrite it!"
        )

    def test_dry_run_installer_reports_db_preserved(self, tmp_path):
        """Installer output explicitly mentions DB preservation when forum.db exists."""
        service_dir = tmp_path / "forum"
        service_dir.mkdir()
        (service_dir / "app").mkdir()
        db_file = service_dir / "forum.db"
        db_file.write_bytes(b"existing_db")

        result = _run_installer([
            "--src", str(_REPO_ROOT),
            "--service-dir", str(service_dir),
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        # The installer prints an explicit "will NOT overwrite" message when forum.db exists.
        assert "NOT overwrite" in combined or "preserved" in combined.lower(), (
            f"Expected DB-preservation message in output when forum.db exists:\n{combined}"
        )


# ---------------------------------------------------------------------------
# Missing --src validation.
# ---------------------------------------------------------------------------

class TestMissingSrc:
    def test_missing_src_exits_nonzero(self, tmp_path):
        result = _run_installer([
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        assert result.returncode != 0, (
            f"Missing --src should exit non-zero, got {result.returncode}"
        )

    def test_missing_src_error_message(self, tmp_path):
        result = _run_installer([
            "--service-dir", str(tmp_path / "forum"),
            "--group", "agents", "--dry-run",
        ])
        combined = result.stdout + result.stderr
        assert "src" in combined.lower() or "required" in combined.lower(), (
            f"Expected --src required error:\n{combined}"
        )


# ---------------------------------------------------------------------------
# #3312: baton login map flags, no-silent-drop refusal, explicit --group.
#
# Every refusal asserts: exit code, stderr content, and that the pre-existing
# unit file is byte-identical. The "nothing written" half runs the installer
# WITHOUT --dry-run against a PATH of stubs (systemctl / rsync / python3 /
# loginctl / curl all exit 97 and log themselves), so a refusal that fires too
# late - or not at all - is visible as a stub call or a rewritten unit, while
# no real systemd / venv / network action can ever run.
# ---------------------------------------------------------------------------

_BEGIN = "--- rendered unit begin ---"
_END = "--- rendered unit end ---"
_MAP = "/etc/engram-forum/baton-login-map.json"


def _stub_path(tmp_path: Path, primary_group: str = "agents") -> tuple[Path, Path]:
    """Return (stub_dir, call_log). `id` is faked; everything else mutating fails loud."""
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    log = tmp_path / "stub-calls.log"
    for name in ("systemctl", "rsync", "python3", "loginctl", "curl", "cp"):
        f = stub / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 97\n')
        f.chmod(0o755)
    # #1459 round 2: the installer decides "loopback" by PARSING the host with python3's
    # ipaddress, and a python3 that fails means "not loopback" (fail-safe). So the read-only
    # loopback probe (`python3 -B -c ...`, #1459 round 4) is passed through to the real interpreter; every OTHER python3 call
    # (`-m venv`, anything mutating) still fails loud and is logged, as before.
    real_py = shutil.which("python3") or sys.executable
    (stub / "python3").write_text(
        '#!/bin/sh\n'
        'case "$1 $2 $3" in\n'
        f'  "-B -c import ipaddress,sys;"*) exec "{real_py}" "$@" ;;\n'
        'esac\n'
        f'echo "python3 $*" >> "{log}"\nexit 97\n')
    real_id = subprocess.run(["which", "id"], capture_output=True, text=True).stdout.strip()
    idf = stub / "id"
    idf.write_text(
        "#!/bin/sh\n"
        f'case "$1" in -gn) echo {primary_group};; -nG) echo {primary_group};; '
        f'*) exec {real_id} "$@";; esac\n'
    )
    idf.chmod(0o755)
    return stub, log


def _install(tmp_path: Path, extra: list[str], *, group: str | None = "agents",
             dry_run: bool = True, primary_group: str = "agents", unit: str | None = None):
    home = tmp_path / "home"
    unit_path = home / ".config" / "systemd" / "user" / "engram-forum.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    if unit is not None:
        unit_path.write_text(unit)
    stub, log = _stub_path(tmp_path, primary_group)
    env = dict(os.environ, HOME=str(home), PATH=f"{stub}{os.pathsep}{os.environ['PATH']}")
    args = ["--src", str(_REPO_ROOT), "--service-dir", str(tmp_path / "forum")]
    if group is not None:
        args += ["--group", group]
    if dry_run:
        args.append("--dry-run")
    result = _run_installer(args + extra, env=env)
    return result, unit_path, log


def _rendered(stdout: str) -> str:
    assert _BEGIN in stdout and _END in stdout, f"no rendered unit in output:\n{stdout}"
    return stdout.split(_BEGIN, 1)[1].split(_END, 1)[0]


def _noncomment(text: str) -> str:
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith(("#", ";"))
    )


_EXISTING_EXEC = (
    "[Service]\nExecStart=/x/python -m forum.server \\\n"
    "    --port 5002 \\\n    --baton-login-map /old/map.json\n"
)


class TestBatonLoginMapFlag:
    def test_flag_rendered_as_real_execstart_args(self, tmp_path):
        r, _, _ = _install(tmp_path, [
            "--baton-login-map", _MAP,
            "--baton-login-map-admin-uid", "1001",
            "--baton-login-map-admin-uid", "1002",
        ])
        assert r.returncode == 0, r.stdout + r.stderr
        body = _noncomment(_rendered(r.stdout))
        assert f"--baton-login-map {_MAP}" in body
        assert "--baton-login-map-admin-uid 1001" in body
        assert "--baton-login-map-admin-uid 1002" in body
        # Valid continuation: the line before each flag line ends in a backslash.
        lines = body.splitlines()
        for i, ln in enumerate(lines):
            if "--baton-login-map" in ln:
                assert lines[i - 1].rstrip().endswith("\\"), (lines[i - 1], ln)
        assert "{{" not in body

    def test_no_flag_no_existing_unit_has_no_token(self, tmp_path):
        r, _, _ = _install(tmp_path, [])
        assert r.returncode == 0, r.stdout + r.stderr
        body = _noncomment(_rendered(r.stdout))
        assert "baton-login-map" not in body.lower()
        assert "ENGRAM_BATON_LOGIN_MAP" not in body
        assert "--audit" in body  # ExecStart still intact

    @pytest.mark.parametrize("bad", ["relative/map.json", "/etc/with space/map.json",
                                     "/etc/map;rm.json", "/etc/ma$p.json", ""])
    def test_bad_path_refused(self, tmp_path, bad):
        r, _, _ = _install(tmp_path, ["--baton-login-map", bad])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "baton-login-map" in r.stderr

    @pytest.mark.parametrize("bad", ["abc", "10x", "-1", ""])
    def test_non_integer_uid_refused(self, tmp_path, bad):
        r, _, _ = _install(tmp_path, ["--baton-login-map", _MAP,
                                      "--baton-login-map-admin-uid", bad])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "uid" in r.stderr.lower()

    def test_uid_without_map_refused(self, tmp_path):
        r, _, _ = _install(tmp_path, ["--baton-login-map-admin-uid", "1001"])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "--baton-login-map" in r.stderr


class TestExistingMapNotSilentlyDropped:
    @pytest.mark.parametrize("dry_run", [True, False])
    @pytest.mark.parametrize("existing", [
        _EXISTING_EXEC,
        "[Service]\nEnvironment=ENGRAM_BATON_LOGIN_MAP=/old/map.json\n",
        '[Service]\nEnvironment="ENGRAM_BATON_LOGIN_MAP=/old/map.json"\n',
        "[Service]\nEnvironment=ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS=1001\n",
        "[Service]\nExecStart=/x \\\n    --baton-login-map-admin-uid 1001\n",
        "[Service]\nExecStart=/x --baton-login-map=/old/map.json\n",
    ], ids=["exec", "env", "env-quoted", "env-uids", "exec-uid", "exec-equals"])
    def test_refuses_without_flag_and_writes_nothing(self, tmp_path, existing, dry_run):
        r, unit, log = _install(tmp_path, [], unit=existing, dry_run=dry_run)
        assert r.returncode == 2, r.stdout + r.stderr
        assert str(unit) in r.stderr
        assert "--baton-login-map" in r.stderr and "re-run" in r.stderr.lower()
        assert unit.read_text() == existing
        assert not log.exists(), f"refusal must precede every action: {log.read_text()}"
        assert not (tmp_path / "forum").exists()

    def test_refusal_reached_under_isolated_home_4393(self, tmp_path):
        """#4393 guard stays exercised: a unit carrying a login map, planted in the
        scratch HOME, still makes --dry-run refuse with the drop-warning text."""
        r, unit, log = _install(tmp_path, [], unit=_EXISTING_EXEC)
        assert r.returncode != 0, r.stdout + r.stderr
        assert "already carries a baton login map" in r.stderr
        assert str(unit).startswith(str(tmp_path))


    def test_same_path_flag_proceeds(self, tmp_path):
        r, unit, _ = _install(tmp_path, ["--baton-login-map", "/old/map.json"],
                              unit=_EXISTING_EXEC)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "--baton-login-map /old/map.json" in _noncomment(_rendered(r.stdout))

    def test_different_path_flag_new_value_wins(self, tmp_path):
        r, _, _ = _install(tmp_path, ["--baton-login-map", _MAP], unit=_EXISTING_EXEC)
        assert r.returncode == 0, r.stdout + r.stderr
        body = _noncomment(_rendered(r.stdout))
        assert f"--baton-login-map {_MAP}" in body and "/old/map.json" not in body

    def test_commented_occurrences_do_not_trigger(self, tmp_path):
        existing = (
            "[Service]\n#Environment=ENGRAM_BATON_LOGIN_MAP=<admin-chosen path>\n"
            "#Environment=ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS=<uid>\n"
            "ExecStart=/x --port 5002\n    # --baton-login-map /nope\n  ; --baton-login-map-admin-uid 5\n"
        )
        r, _, _ = _install(tmp_path, [], unit=existing)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_existing_template_rendered_unit_roundtrips(self, tmp_path):
        """A unit this installer rendered with a map re-installs only with the flag."""
        r1, _, _ = _install(tmp_path, ["--baton-login-map", _MAP,
                                       "--baton-login-map-admin-uid", "1001"])
        rendered = _rendered(r1.stdout)
        r2, unit, _ = _install(tmp_path, [], unit=rendered)
        assert r2.returncode == 2 and str(unit) in r2.stderr

    def test_uid_in_existing_unit_not_dropped(self, tmp_path):
        existing = _EXISTING_EXEC + "    --baton-login-map-admin-uid 1001\n"
        r, unit, log = _install(tmp_path, ["--baton-login-map", "/old/map.json"],
                                unit=existing, dry_run=False)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "admin-uid" in r.stderr and str(unit) in r.stderr
        assert unit.read_text() == existing
        assert not log.exists()

    def test_uid_in_existing_unit_with_uid_flag_proceeds(self, tmp_path):
        existing = _EXISTING_EXEC + "    --baton-login-map-admin-uid 1001\n"
        r, _, _ = _install(tmp_path, ["--baton-login-map", "/old/map.json",
                                      "--baton-login-map-admin-uid", "1001"], unit=existing)
        assert r.returncode == 0, r.stdout + r.stderr


class TestExplicitGroup:
    def test_omitted_group_refused_when_primary_not_agents(self, tmp_path):
        r, unit, log = _install(tmp_path, [], group=None, primary_group="wheel")
        assert r.returncode == 2, r.stdout + r.stderr
        assert "--group" in r.stderr and "wheel" in r.stderr
        assert not (tmp_path / "forum").exists()

    def test_omitted_group_ok_when_primary_is_agents(self, tmp_path):
        r, _, _ = _install(tmp_path, [], group=None, primary_group="agents")
        assert r.returncode == 0, r.stdout + r.stderr

    def test_explicit_group_never_refused_for_primary(self, tmp_path):
        r, _, _ = _install(tmp_path, [], group="agents", primary_group="wheel")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "primary" not in r.stderr.lower()


class TestSystemBashCompat:
    """The installer must run under the SYSTEM bash, which on macOS is 3.2.57.

    `bash -n` cannot catch this class: 3.2 parses `$( )` bodies lazily, so a
    construct it mis-parses (a `case` pattern's `)` or quotes inside `${...}`
    in a command substitution) only fails at run time. The rest of this file runs
    `bash` from PATH, which on a Mac is often Homebrew's bash 5 and hides it.
    Only a seat on a real Mac exercises the 3.2 path (no macOS CI runner); on
    Linux /bin/bash is 4+/5 and this is an ordinary smoke run.
    """

    @pytest.mark.skipif(not Path("/bin/bash").exists(), reason="no /bin/bash")
    def test_dry_run_with_map_renders_under_system_bash(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home))
        r = subprocess.run(
            ["/bin/bash", str(_INSTALLER), "--src", str(_REPO_ROOT),
             "--service-dir", str(tmp_path / "forum"), "--group", "agents",
             "--baton-login-map", _MAP, "--baton-login-map-admin-uid", "1001",
             "--dry-run"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        unit = _rendered(r.stdout)
        assert f"--baton-login-map {_MAP}" in unit
        assert "--baton-login-map-admin-uid 1001" in unit


class TestGuardAndWriteShareOneUnitPath:
    """The C2 guard must read the very file the installer later replaces (#3312, S1).

    A guard that keeps its own literal can drift from the write path (a future
    --unit-dir / XDG_CONFIG_HOME change) and then silently check a file that is
    never replaced. One definition of UNIT_DIR/UNIT, used by both.
    """

    def _src(self) -> str:
        return _INSTALLER.read_text()

    def test_unit_path_literal_is_defined_once(self):
        assert self._src().count("systemd/user") == 1

    def test_guard_derives_from_unit_dir_and_unit(self):
        src = self._src()
        assert '_EXISTING_UNIT="$UNIT_DIR/$UNIT"' in src
        # and the definition precedes the guard that uses it
        assert src.index('UNIT_DIR="$HOME/.config/systemd/user"') < src.index("_EXISTING_UNIT=")
