"""Contract cells for the forum deploy shape (engram-alpha#3568: F2, F3, F4).

Written BEFORE the implementation (contract-first, #3045). The contract:

  1. The backup and alarm units are RENDERED from templates ({{FORUM_HOME}}),
     never copied, and name neither our shared directory nor a checkout path.
     tools/forum_backup.py is snapshotted into <service-dir>/app/tools/.
  2. A kept (hand-edited) backup service/timer is never overwritten; a kept unit
     that differs from what would be installed prints exactly one notice.
  3. Loud on a dead target: after the venv and snapshot exist and before any
     systemctl action, every absolute path in the LIVE backup unit's ExecStart
     must exist (interpreter + script as files, the parent dir of --db /
     --backup-dir / --audit), else a non-zero exit that names the path.
  4. No --service-dir means the FORUM_HOME of the existing engram-forum.service;
     no unit refuses (exit 2); an unparsable unit refuses naming the unit file.
  5. The --group refusal does not name `agents` as the expected value.

HERMETIC: every subprocess gets its environment from ``_hermetic_env``, which pins
HOME and XDG_CONFIG_HOME to a temp dir and drops ENGRAM_HOME: on the forum-admin
seat the real ~/.config/systemd/user/engram-forum.service is right there, and a
test that reads it passes on one seat and fails on another (#3561).

SAFETY: a cell that passes no --service-dir is ONLY ever run with --dry-run, so a
red run against code that still defaults to the shared directory cannot write there.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[3]
DEPLOY = _REPO / "src" / "forum" / "deploy"
INSTALLER = DEPLOY / "install-forum-service.sh"
BACKUP_TEMPLATE = DEPLOY / "engram-forum-backup.service.template"
ALARM_TEMPLATE = DEPLOY / "engram-forum-backup-alarm.service.template"
TIMER = DEPLOY / "engram-forum-backup.timer"
MAIN_TEMPLATE = DEPLOY / "engram-forum.service.template"

BACKUP = "engram-forum-backup.service"
PROBE = "probe-3568.service"  # the unit the function cells check: independent of keep/overwrite
ALARM = "engram-forum-backup-alarm.service"
TIMER_NAME = "engram-forum-backup.timer"
MAIN = "engram-forum.service"
DOC_URL = "https://github.com/engram-agents/engram/blob/main/src/forum/deploy/README.md"
OLD_DOC_URL = "https://github.com/engram-agents/engram/blob/main/forum/deploy/README.md"


# ---------------------------------------------------------------- helpers ----

def _hermetic_env(home: Path, path_prefix: Path | None = None, **extra) -> dict:
    """The ONE environment every subprocess here runs under."""
    env = dict(os.environ)
    env.pop("ENGRAM_HOME", None)
    env.pop("FORUM_BACKUP_ALARM_MENTION", None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    env.update(extra)
    return env


def _my_group() -> str:
    return subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()


def _stubs(tmp_path: Path, primary_group: str | None = None) -> tuple[Path, Path]:
    """Stubs for the systemd-facing commands (they log and succeed) so a real run
    never touches a real user manager; optionally a fake `id -gn`."""
    stub = tmp_path / "stubs"
    stub.mkdir(exist_ok=True)
    log = tmp_path / "stub-calls.log"
    for name in ("systemctl", "loginctl", "curl"):
        f = stub / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
        f.chmod(0o755)
    if primary_group is not None:
        real_id = subprocess.run(["which", "id"], capture_output=True, text=True).stdout.strip()
        idf = stub / "id"
        idf.write_text(
            "#!/bin/sh\n"
            f'case "$1" in -gn) echo {primary_group};; -nG) echo {primary_group};; '
            f'*) exec {real_id} "$@";; esac\n'
        )
        idf.chmod(0o755)
    return stub, log


def _digest(root: Path) -> list:
    out = []
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out.append((rel, "link", os.readlink(p)))
        elif p.is_dir():
            out.append((rel, "dir", ""))
        else:
            out.append((rel, "file", hashlib.sha256(p.read_bytes()).hexdigest()))
    return out


def _prep_service_dir(svc: Path) -> None:
    """An existing, working deployment shell: app/ and a venv whose python is a
    stub that exits 0 (so the installer reuses it and builds nothing)."""
    (svc / "app").mkdir(parents=True)
    py = svc / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)


def _execstart(svc: Path, script: str | None = None, extra: str = "") -> str:
    script = script or f"{svc}/app/tools/forum_backup.py"
    return (
        "[Unit]\nDescription=hand-edited\n\n[Service]\nType=oneshot\n"
        f"ExecStart={svc}/.venv/bin/python \\\n    {script} \\\n"
        f"    --db {svc}/forum.db \\\n    --backup-dir {svc}/backup \\\n"
        f"    --audit {svc}/forum-audit.jsonl{extra}\n"
    )


def _install(tmp_path: Path, *, service_dir_arg: bool = True, dry_run: bool = False,
             no_start: bool = True, prep: bool = True, existing: dict | None = None,
             primary_group: str | None = None, group: str | None = "auto",
             extra_args: tuple = (), svc_name: str = "forum") -> SimpleNamespace:
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    svc = tmp_path / svc_name
    if prep:
        _prep_service_dir(svc)
    for name, text in (existing or {}).items():
        (unit_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (unit_dir / name).write_text(text)
    stub, log = _stubs(tmp_path, primary_group)
    args = ["bash", str(INSTALLER), "--src", str(_REPO)]
    if service_dir_arg:
        args += ["--service-dir", str(svc)]
    if group == "auto":
        group = _my_group()
    if group is not None:
        args += ["--group", group]
    if dry_run:
        args.append("--dry-run")
    if no_start:
        args.append("--no-start")
    args += list(extra_args)
    r = subprocess.run(args, capture_output=True, text=True,
                       env=_hermetic_env(home, stub))
    return SimpleNamespace(r=r, home=home, unit_dir=unit_dir, svc=svc, log=log,
                           out=r.stdout, err=r.stderr, both=r.stdout + r.stderr)


def _log(ctx) -> str:
    return ctx.log.read_text() if ctx.log.exists() else ""


# ---------------------------------------------------------------------------
# Cell 1 -- rendered, never copied
# ---------------------------------------------------------------------------

class TestRenderedBackupUnits:

    @pytest.mark.parametrize("unit", [BACKUP, ALARM])
    def test_installed_unit_names_the_service_dir_and_neither_our_dir_nor_a_checkout(
            self, tmp_path, unit):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        text = (ctx.unit_dir / unit).read_text()
        assert str(ctx.svc) in text
        assert "/home/agents-shared" not in text, text
        assert "engram-alpha" not in text, text
        assert "%h/" not in text, text
        assert "{{" not in text, text

    def test_backup_execstart_is_the_snapshot_under_the_service_dir(self, tmp_path):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        text = re.sub(r"\s*\\\n\s*", " ", (ctx.unit_dir / BACKUP).read_text())
        svc = ctx.svc
        assert f"ExecStart={svc}/.venv/bin/python {svc}/app/tools/forum_backup.py " in text
        assert f"--db {svc}/forum.db" in text
        assert f"--backup-dir {svc}/backup" in text
        assert f"--audit {svc}/forum-audit.jsonl" in text

    def test_alarm_execstart_is_the_snapshot_and_keeps_its_flags(self, tmp_path):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        text = re.sub(r"\s*\\\n\s*", " ", (ctx.unit_dir / ALARM).read_text())
        svc = ctx.svc
        assert (f"ExecStart={svc}/.venv/bin/python {svc}/app/tools/forum_backup.py "
                "--alarm --unit engram-forum-backup.service") in text

    def test_forum_backup_is_snapshotted_as_a_real_file_not_a_link(self, tmp_path):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        snap = ctx.svc / "app" / "tools" / "forum_backup.py"
        assert snap.is_file() and not snap.is_symlink()
        assert snap.read_bytes() == (_REPO / "tools" / "forum_backup.py").read_bytes()

    def test_dropins_are_still_written(self, tmp_path):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        alarm = ctx.unit_dir / f"{BACKUP}.d" / "alarm.conf"
        assert alarm.read_text() == "[Unit]\nOnFailure=engram-forum-backup-alarm.service\n"
        assert (ctx.unit_dir / f"{ALARM}.d" / "mention.conf").read_text().startswith("[Service]\n")

    def test_the_timer_is_installed_byte_identical_to_the_shipped_file(self, tmp_path):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        assert (ctx.unit_dir / TIMER_NAME).read_bytes() == TIMER.read_bytes()

    def test_an_existing_timer_is_untouched(self, tmp_path):
        custom = "[Timer]\nOnCalendar=hourly\n"
        ctx = _install(tmp_path, existing={TIMER_NAME: custom})
        assert ctx.r.returncode == 0, ctx.both
        assert (ctx.unit_dir / TIMER_NAME).read_text() == custom

    def test_the_alarm_unit_is_re_rendered_every_run(self, tmp_path):
        ctx = _install(tmp_path, existing={ALARM: "stale\n"})
        assert ctx.r.returncode == 0, ctx.both
        text = (ctx.unit_dir / ALARM).read_text()
        assert text != "stale\n" and str(ctx.svc) in text

    def test_shipped_backup_templates_carry_no_team_path(self):
        for t in (BACKUP_TEMPLATE, ALARM_TEMPLATE):
            assert t.is_file(), f"{t.name} missing"
            text = t.read_text()
            assert "{{FORUM_HOME}}" in text
            assert "/home/agents-shared" not in text, t.name
            assert "%h/engram-alpha" not in text and "engram-alpha" not in text, t.name


# ---------------------------------------------------------------------------
# Cell 2 -- dry-run prints the render and writes nothing
# ---------------------------------------------------------------------------

class TestDryRun:

    def test_dry_run_prints_the_render_and_writes_nothing(self, tmp_path):
        ctx = _install(tmp_path, dry_run=True)
        assert ctx.r.returncode == 0, ctx.both
        svc = ctx.svc
        printed = re.sub(r"\s*\\\n\s*", " ", ctx.out)
        assert f"{svc}/.venv/bin/python {svc}/app/tools/forum_backup.py" in printed
        assert "/home/agents-shared" not in ctx.out
        assert "engram-alpha" not in ctx.out.replace(str(_REPO), "")

    def test_dry_run_leaves_both_trees_byte_identical(self, tmp_path):
        # Build the trees, then run again in dry-run against a fresh digest.
        home = tmp_path / "home"
        (home / ".config" / "systemd" / "user").mkdir(parents=True)
        svc = tmp_path / "forum"
        _prep_service_dir(svc)
        before = (_digest(home), _digest(svc))
        ctx = _install(tmp_path, dry_run=True, prep=False)
        assert ctx.r.returncode == 0, ctx.both
        assert (_digest(home), _digest(svc)) == before

    def test_dry_run_says_it_would_check_every_execstart_path(self, tmp_path):
        ctx = _install(tmp_path, dry_run=True)
        assert ctx.r.returncode == 0, ctx.both
        assert (f"[dry-run] would check every ExecStart path of {ctx.unit_dir / BACKUP}"
                in ctx.out), ctx.out

    def test_dry_run_with_a_missing_service_dir_creates_nothing(self, tmp_path):
        ctx = _install(tmp_path, dry_run=True, prep=False)
        assert ctx.r.returncode == 0, ctx.both
        assert not ctx.svc.exists()


# ---------------------------------------------------------------------------
# Cells 3 / 4 -- kept units, loud on a dead target
# ---------------------------------------------------------------------------

class TestKeptBackupUnit:

    def test_kept_unit_with_a_missing_script_refuses_and_restarts_nothing(self, tmp_path):
        svc = tmp_path / "forum"
        dead = "/nonexistent-3568/forum_backup.py"
        unit = _execstart(svc, script=dead)
        ctx = _install(tmp_path, no_start=False, existing={BACKUP: unit})
        assert ctx.r.returncode != 0, ctx.both
        assert dead in ctx.both
        log = _log(ctx)
        assert not re.search(r"\b(restart|enable|start)\b", log), log
        assert (ctx.unit_dir / BACKUP).read_text() == unit

    def test_kept_unit_whose_paths_all_exist_is_kept_and_a_notice_is_printed(self, tmp_path):
        svc = tmp_path / "forum"
        unit = _execstart(svc, extra=" \\\n    --packs-dir /srv/packs")
        ctx = _install(tmp_path, existing={BACKUP: unit})
        assert ctx.r.returncode == 0, ctx.both
        assert (ctx.unit_dir / BACKUP).read_bytes() == unit.encode()
        notes = [ln for ln in ctx.both.splitlines() if "differs from" in ln]
        assert len(notes) == 1, ctx.both
        assert str(ctx.unit_dir / BACKUP) in notes[0] or BACKUP in notes[0]

    def test_kept_unit_identical_to_the_render_prints_no_notice(self, tmp_path):
        first = _install(tmp_path)
        assert first.r.returncode == 0, first.both
        second = _install(tmp_path, prep=False)
        assert second.r.returncode == 0, second.both
        assert "differs from" not in second.both, second.both

    def test_kept_unit_with_a_missing_script_is_refused_before_any_systemctl(self, tmp_path):
        """No systemctl action of any kind precedes the refusal."""
        svc = tmp_path / "forum"
        ctx = _install(tmp_path, no_start=False,
                       existing={BACKUP: _execstart(svc, script="/nonexistent-3568/x.py")})
        assert ctx.r.returncode != 0
        assert _log(ctx) == "", _log(ctx)

    def test_fresh_install_passes_the_check_and_proceeds_to_start(self, tmp_path):
        """The check runs AFTER the snapshot exists: a fresh install must pass it
        and reach the restart (stubbed)."""
        ctx = _install(tmp_path, no_start=False)
        assert ctx.r.returncode == 0, ctx.both
        assert "restart" in _log(ctx)


# ---------------------------------------------------------------------------
# The check itself, driven through the marked region (parse + parent cells)
# ---------------------------------------------------------------------------

_REGION = re.compile(r"^# >>> backup-units\n(.*?)^# <<< backup-units\n", re.S | re.M)


def _check(tmp_path: Path, unit_text: str, *, home_files: tuple = ()) -> SimpleNamespace:
    text = INSTALLER.read_text(encoding="utf-8")
    m = _REGION.search(text)
    assert m, "installer must keep the '# >>> backup-units' / '# <<< backup-units' markers"
    assert "_check_backup_exec_paths" in m.group(1), (
        "the dead-target check must live in ONE function inside the marked region")
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    svc = tmp_path / "forum"
    svc.mkdir()
    for rel in home_files:
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    (unit_dir / PROBE).write_text(unit_text)
    script = (
        "set -euo pipefail\n"
        f"SRC={shlex.quote(str(_REPO))}\nUNIT_DIR={shlex.quote(str(unit_dir))}\n"
        f"SERVICE_DIR={shlex.quote(str(svc))}\nDRY_RUN=0\n"
        'RUN() { "$@"; }\n' + m.group(1) +
        f'\n_check_backup_exec_paths "$UNIT_DIR/{PROBE}"\necho CHECK-PASSED\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=_hermetic_env(home))
    return SimpleNamespace(r=r, svc=svc, home=home, unit_dir=unit_dir,
                           both=r.stdout + r.stderr, passed="CHECK-PASSED" in r.stdout)


def _live_unit(svc: Path, *, db="forum.db", backup="backup", audit="audit.jsonl",
               interp=None, script=None, joiner=" ", eq=False) -> str:
    interp = interp or "/bin/sh"
    script = script or "/bin/sh"
    sep = "=" if eq else " "
    return (
        f"[Service]\nExecStart={interp}{joiner}{script}{joiner}"
        f"--db{sep}{svc}/{db}{joiner}--backup-dir{sep}{svc}/{backup}{joiner}"
        f"--audit{sep}{svc}/{audit}\n"
    )


class TestDeadTargetCheck:

    def test_all_paths_present_passes(self, tmp_path):
        c = _check(tmp_path, _live_unit(tmp_path / "forum"))
        assert c.passed, c.both

    def test_backslash_continuations_are_joined(self, tmp_path):
        c = _check(tmp_path, _live_unit(tmp_path / "forum", joiner=" \\\n    "))
        assert c.passed, c.both

    def test_equals_form_flags_are_checked(self, tmp_path):
        c = _check(tmp_path, _live_unit(tmp_path / "forum", eq=True))
        assert c.passed, c.both
        c2 = _check(tmp_path / "b", _live_unit(tmp_path / "b" / "forum", eq=True,
                                              db="nope/forum.db"))
        assert not c2.passed and str(tmp_path / "b" / "forum" / "nope") in c2.both, c2.both

    def test_exec_prefix_modifier_is_tolerated(self, tmp_path):
        u = _live_unit(tmp_path / "forum").replace("ExecStart=/bin/sh", "ExecStart=-/bin/sh")
        assert _check(tmp_path, u).passed

    def test_missing_interpreter_is_named(self, tmp_path):
        c = _check(tmp_path, _live_unit(tmp_path / "forum", interp="/nonexistent-3568/python"))
        assert not c.passed and c.r.returncode != 0
        assert "/nonexistent-3568/python" in c.both

    def test_missing_script_is_named(self, tmp_path):
        c = _check(tmp_path, _live_unit(tmp_path / "forum", script="/nonexistent-3568/s.py"))
        assert not c.passed and "/nonexistent-3568/s.py" in c.both

    def test_the_script_must_be_a_file_not_a_directory(self, tmp_path):
        c = _check(tmp_path, _live_unit(tmp_path / "forum", script="/tmp"))
        assert not c.passed and "/tmp" in c.both

    @pytest.mark.parametrize("flag", ["--db", "--backup-dir", "--audit"])
    @pytest.mark.parametrize("eq", [False, True], ids=["space", "equals"])
    def test_missing_parent_of_each_flag_is_named(self, tmp_path, flag, eq):
        svc = tmp_path / "forum"
        key = {"--db": "db", "--backup-dir": "backup", "--audit": "audit"}[flag]
        u = _live_unit(svc, **{key: "gone-3568/leaf"}, eq=eq)
        c = _check(tmp_path, u)
        assert not c.passed and c.r.returncode != 0, c.both
        assert str(svc / "gone-3568") in c.both, c.both

    def test_the_leaf_of_a_flag_need_not_exist_only_its_parent(self, tmp_path):
        c = _check(tmp_path, _live_unit(tmp_path / "forum", db="not-yet.db", backup="not-yet-dir"))
        assert c.passed, c.both

    def test_percent_h_is_expanded_in_the_message_and_the_check(self, tmp_path):
        u = ("[Service]\nExecStart=%h/bin/py %h/tools/s.py "
             f"--db {tmp_path}/forum/forum.db\n")
        ok = _check(tmp_path / "a", u.replace(f"{tmp_path}", f"{tmp_path}/a"),
                    home_files=("bin/py", "tools/s.py"))
        assert ok.passed, ok.both
        bad = _check(tmp_path / "b", u.replace(f"{tmp_path}", f"{tmp_path}/b"),
                     home_files=("bin/py",))
        assert not bad.passed
        assert f"{tmp_path}/b/home/tools/s.py" in bad.both, bad.both
        assert "%h/tools/s.py" not in bad.both, bad.both

    @pytest.mark.parametrize("body", [
        "[Service]\nType=oneshot\n",
        "[Service]\nExecStart=\n",
        "[Service]\n# ExecStart=/bin/sh /bin/sh\n; ExecStart=/bin/sh /bin/sh\n",
        "[Service]\nExecStart=python3 /bin/sh\n",
        "[Service]\nExecStart=/bin/sh\n",
    ], ids=["absent", "empty", "only-comments", "relative-interpreter", "no-script"])
    def test_absent_or_unparseable_execstart_is_a_refusal_naming_the_unit(self, tmp_path, body):
        c = _check(tmp_path, body)
        assert not c.passed and c.r.returncode != 0, c.both
        assert PROBE in c.both and "ExecStart" in c.both, c.both


# ---------------------------------------------------------------------------
# Cells 5 / 6 -- no baked-in service dir
# ---------------------------------------------------------------------------

_MAIN_BASE = "[Unit]\nDescription=x\n\n[Service]\n{env}\nExecStart=/x/python -m forum.server --port 5002\n"


class TestServiceDirResolution:

    @pytest.mark.parametrize("env_line", [
        "Environment=FORUM_HOME=/srv/f",
        'Environment="FORUM_HOME=/srv/f"',
    ], ids=["plain", "quoted"])
    def test_existing_units_forum_home_is_used(self, tmp_path, env_line):
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False,
                       existing={MAIN: _MAIN_BASE.format(env=env_line)})
        assert ctx.r.returncode == 0, ctx.both
        assert "Forum service dir: /srv/f " in ctx.out, ctx.out
        assert "/srv/f/.venv/bin/python" in ctx.out
        assert "/home/agents-shared" not in ctx.out

    def test_an_explicit_flag_wins_over_the_existing_unit(self, tmp_path):
        ctx = _install(tmp_path, dry_run=True, existing={
            MAIN: _MAIN_BASE.format(env="Environment=FORUM_HOME=/srv/f")})
        assert ctx.r.returncode == 0, ctx.both
        assert f"Forum service dir: {ctx.svc} " in ctx.out
        assert "/srv/f" not in ctx.out

    def test_no_flag_and_no_unit_refuses_with_exit_2_and_writes_nothing(self, tmp_path):
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False)
        before = _digest(ctx.home)
        assert ctx.r.returncode == 2, ctx.both
        assert ("--service-dir is required on a first install (e.g. /home/<shared>/forum)"
                in ctx.err), ctx.err
        assert "Forum service dir" not in ctx.out
        assert _digest(ctx.home) == before
        assert _log(ctx) == ""

    @pytest.mark.parametrize("body", [
        "[Service]\nExecStart=/x/python -m forum.server\n",
        "[Service]\nEnvironment=FORUM_HOME=\nExecStart=/x\n",
        "[Service]\n# Environment=FORUM_HOME=/srv/commented\nExecStart=/x\n",
        '[Service]\nEnvironment="FORUM_HOME="\nExecStart=/x\n',
    ], ids=["absent", "empty", "commented", "quoted-empty"])
    def test_unparsable_existing_unit_refuses_naming_the_unit_never_guesses(self, tmp_path, body):
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False,
                       existing={MAIN: body})
        assert ctx.r.returncode == 2, ctx.both
        assert str(ctx.unit_dir / MAIN) in ctx.err, ctx.err
        assert "required on a first install" not in ctx.err, (
            "an unparsable unit is NOT a first install; it must not take that path")
        assert "Forum service dir" not in ctx.out


# ---------------------------------------------------------------------------
# Cell 8 -- --group text for a general user
# ---------------------------------------------------------------------------

class TestGroupMessage:

    def test_refusal_does_not_name_agents_and_carries_the_new_sentence(self, tmp_path):
        ctx = _install(tmp_path, dry_run=True, group=None, primary_group="wheel")
        assert ctx.r.returncode == 2, ctx.both
        assert ("--group not given: pass --group <the shared group your forum seats belong to>"
                in ctx.err), ctx.err
        assert "agents" not in ctx.err.lower(), ctx.err

    def test_primary_group_literally_agents_still_passes_without_the_flag(self, tmp_path):
        ctx = _install(tmp_path, dry_run=True, group=None, primary_group="agents")
        assert ctx.r.returncode == 0, ctx.both

    def test_installer_header_does_not_bake_in_our_defaults(self):
        head = INSTALLER.read_text().split("set -euo pipefail", 1)[0]
        assert "Default: /home/agents-shared/forum" not in head
        assert 'SERVICE_DIR="/home/agents-shared/forum"' not in INSTALLER.read_text()


# ---------------------------------------------------------------------------
# Documentation= URL (all three unit templates)
# ---------------------------------------------------------------------------

class TestDocumentationUrl:

    @pytest.mark.parametrize("path", [MAIN_TEMPLATE, BACKUP_TEMPLATE, TIMER],
                             ids=lambda p: p.name)
    def test_points_at_the_path_that_exists_in_the_public_repo(self, path):
        text = path.read_text()
        assert f"Documentation={DOC_URL}\n" in text, path.name
        assert OLD_DOC_URL not in text.replace(DOC_URL, ""), path.name


# ---------------------------------------------------------------------------
# #3568 follow-up -- _unit_forum_home must not accept a multi-assignment line
# ---------------------------------------------------------------------------
# D1: print a value ONLY if absolute and free of whitespace, `"` and `'`.
# D2: the table below. D3: end to end, exit 2 naming the unit, nothing created.

DRIFT = DEPLOY / "forum-drift-check.sh"

_HOME_CASES = [
    ("Environment=FORUM_HOME=/srv/forum FOO=1\n", "", "two-assign-plain"),
    ('Environment="FORUM_HOME=/srv/forum" "FOO=1"\n', "", "two-assign-quoted"),
    ("Environment=FOO=1 FORUM_HOME=/srv/forum\n", "", "forum-home-second"),
    ("Environment=FORUM_HOME=/srv/forum\n", "/srv/forum", "plain"),
    ('Environment="FORUM_HOME=/srv/forum"\n', "/srv/forum", "quoted-single"),
    ("Environment=FORUM_HOME=/srv/forum   \n", "/srv/forum", "trailing-spaces"),
    ("Environment=FORUM_HOME=/a\nEnvironment=FORUM_HOME=/b\n", "/b", "last-line-wins"),
    # quote with NO whitespace: the only cells that discriminate "reject quotes"
    # from "reject whitespace" (the two-assign-quoted value also holds spaces)
    ('Environment=FORUM_HOME=/srv/for"um\n', "", "stray-double-quote-no-space"),
    ("Environment=FORUM_HOME=/srv/for'um\n", "", "stray-single-quote-no-space"),
    ("Environment=FORUM_HOME=forum\n", "", "relative"),
    ("Environment=FORUM_HOME=%h/forum\n", "", "specifier"),
]


def _extract_fn(script: Path, name: str) -> str:
    out = subprocess.run(
        ["awk", "/^" + name + "\\(\\) \\{/{p=1} p{print} p&&/^\\}/{exit}", str(script)],
        capture_output=True, text=True, check=True).stdout
    assert out.startswith(name + "() {") and out.rstrip().endswith("}"), out
    return out


def _extract_unit_forum_home(script: Path) -> str:
    return _extract_fn(script, "_unit_forum_home")


def _run_unit_forum_home(script: Path, tmp_path: Path, unit_text: str) -> str:
    unit = tmp_path / "probe.service"
    unit.write_text("[Service]\n" + unit_text + "ExecStart=/x\n")
    func = _extract_unit_forum_home(script)
    r = subprocess.run(["bash", "-c", func + '\n_unit_forum_home "$1"', "x", str(unit)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.rstrip("\n")


class TestUnitForumHomeParser:

    @pytest.mark.parametrize("script", [INSTALLER, DRIFT], ids=lambda p: p.name)
    @pytest.mark.parametrize("text,expected", [(c[0], c[1]) for c in _HOME_CASES],
                             ids=[c[2] for c in _HOME_CASES])
    def test_function_table(self, tmp_path, script, text, expected):
        assert _run_unit_forum_home(script, tmp_path, text) == expected

    def test_the_two_hand_kept_copies_are_byte_identical(self):
        assert _extract_unit_forum_home(INSTALLER) == _extract_unit_forum_home(DRIFT)

    @pytest.mark.parametrize("env_line", [
        "Environment=FORUM_HOME=@BASE@/forum FOO=1",
        'Environment="FORUM_HOME=@BASE@/forum" "FOO=1"',
    ], ids=["plain", "quoted"])
    def test_installer_refuses_a_multi_assignment_unit_and_creates_nothing(
            self, tmp_path, env_line):
        base = tmp_path / "base"
        base.mkdir()
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False,
                       existing={MAIN: _MAIN_BASE.format(
                           env=env_line.replace("@BASE@", str(base)))})
        assert ctx.r.returncode == 2, ctx.both
        assert str(ctx.unit_dir / MAIN) in ctx.err, ctx.err
        assert "Forum service dir" not in ctx.out
        assert list(base.iterdir()) == [], list(base.iterdir())


# ---------------------------------------------------------------------------
# #3579 -- a kept forum unit may contain only the shapes the installer itself
# renders; anything else is a LOUD refusal (exit 2) naming the line and the remedy.
# Written BEFORE the implementation (contract-first).
# ---------------------------------------------------------------------------

def _unit_with_exec(execstart: str) -> str:
    return f"[Service]\nExecStart={execstart}\n"


class TestKeptUnitShapes3579:

    def test_positive_control_the_live_unit_shape_still_passes(self, tmp_path):
        svc = tmp_path / "forum"
        c = _check(tmp_path, _live_unit(svc, joiner=" \\\n    "))
        assert c.passed and c.r.returncode == 0, c.both

    @pytest.mark.parametrize("flag", ["--db", "--backup-dir", "--audit"])
    def test_flag_as_last_token_with_no_value_is_refused_naming_the_flag(self, tmp_path, flag):
        c = _check(tmp_path, _unit_with_exec(f"/bin/sh /bin/sh {flag}"))
        assert not c.passed and c.r.returncode == 2, c.both
        assert flag in c.r.stderr, c.both

    def test_equals_form_with_empty_value_is_refused(self, tmp_path):
        c = _check(tmp_path, _unit_with_exec("/bin/sh /bin/sh --db="))
        assert not c.passed and c.r.returncode == 2, c.both
        assert "--db" in c.r.stderr, c.both

    @pytest.mark.parametrize("exec_line", [
        "/usr/bin/env python3 /bin/sh --db /x",
        "/bin/sh -m forum.server --db /x",
        "/bin/sh -m /bin/sh --db /x",
        "%S/bin/py /bin/sh --db /x",
        "/bin/sh %S/s.py --db /x",
        "/bin/sh /bin/sh --db %S/forum.db",
        "/bin/sh /bin/sh --db $FORUM_HOME/forum.db",
        "$FORUM_HOME/bin/py /bin/sh",
        "/bin/sh $FORUM_HOME/s.py",
    ], ids=["env", "dash-m-module", "dash-m-abs", "specifier-interp", "specifier-script",
            "specifier-flag-value", "var-flag-value", "var-interp", "var-script"])
    def test_foreign_shapes_are_refused_with_the_remedy(self, tmp_path, exec_line):
        c = _check(tmp_path, _unit_with_exec(exec_line))
        assert not c.passed and c.r.returncode == 2, c.both
        err = c.r.stderr
        assert "only" in err and "renders" in err, err
        assert "mv " in err and "re-run" in err, err
        assert PROBE in err, err

    def test_percent_h_keeps_working(self, tmp_path):
        u = _unit_with_exec(f"%h/bin/py %h/tools/s.py --db {tmp_path}/forum/forum.db")
        assert _check(tmp_path, u, home_files=("bin/py", "tools/s.py")).passed

    def test_standalone_semicolon_second_command_is_refused(self, tmp_path):
        c = _check(tmp_path, _unit_with_exec("/bin/sh /bin/sh --db /x ; /bin/true"))
        assert not c.passed and c.r.returncode == 2, c.both
        assert ";" in c.r.stderr and "second command" in c.r.stderr, c.both

    @pytest.mark.parametrize("exec_line", [
        "/bin/sh /bin/sh --db /x; /bin/true",
        "/bin/sh /bin/sh --db /x;",
        "/bin/sh /bin/sh --db /x ;/bin/true",
    ], ids=["glued-trailing", "glued-last", "glued-leading"])
    def test_semicolon_glued_to_a_token_is_refused_too(self, tmp_path, exec_line):
        c = _check(tmp_path, _unit_with_exec(exec_line))
        assert not c.passed and c.r.returncode == 2, c.both

    def test_semicolon_comment_line_inside_a_continuation_still_ignored(self, tmp_path):
        svc = tmp_path / "forum"
        u = (f"[Service]\nExecStart=/bin/sh /bin/sh \\\n; a comment line\n"
             f"# another\n    --db {svc}/forum.db\n")
        c = _check(tmp_path, u)
        assert c.passed, c.both


def _dropin_unit_home(tmp_path: Path, dropin_text: str | None) -> tuple:
    """Main unit with NO FORUM_HOME, plus (optionally) a drop-in."""
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / MAIN).write_text("[Service]\nExecStart=/x\n")
    if dropin_text is not None:
        d = unit_dir / (MAIN + ".d")
        d.mkdir()
        (d / "00-other.conf").write_text("[Service]\nEnvironment=PACK_LIMIT=3\n")
        (d / "home.conf").write_text(dropin_text)
    return home, unit_dir


class TestForumHomeInDropin3579:

    @pytest.mark.parametrize("line", [
        "Environment=FORUM_HOME=/srv/forum",
        'Environment="FORUM_HOME=/srv/forum"',
    ], ids=["plain", "quoted"])
    def test_installer_refuses_and_names_the_dropin(self, tmp_path, line):
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False,
                       existing={MAIN: "[Service]\nExecStart=/x\n",
                                 MAIN + ".d/home.conf": f"[Service]\n{line}\n"})
        assert ctx.r.returncode == 2, ctx.both
        assert str(ctx.unit_dir / (MAIN + ".d") / "home.conf") in ctx.err, ctx.err
        assert "drop-in" in ctx.err and "main unit" in ctx.err, ctx.err
        assert "Forum service dir" not in ctx.out

    @pytest.mark.parametrize("line", [
        "Environment=FORUM_HOME=/srv/forum",
        'Environment="FORUM_HOME=/srv/forum"',
    ], ids=["plain", "quoted"])
    def test_drift_check_refuses_and_names_the_dropin(self, tmp_path, line):
        home, unit_dir = _dropin_unit_home(tmp_path, f"[Service]\n{line}\n")
        r = subprocess.run(["bash", str(DRIFT)], capture_output=True, text=True,
                           env=_hermetic_env(home))
        assert r.returncode == 2, r.stdout + r.stderr
        assert str(unit_dir / (MAIN + ".d") / "home.conf") in r.stderr, r.stderr
        assert "drop-in" in r.stderr and "main unit" in r.stderr, r.stderr

    def test_dropin_with_other_settings_only_keeps_the_plain_unparsable_message(self, tmp_path):
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False,
                       existing={MAIN: "[Service]\nExecStart=/x\n",
                                 MAIN + ".d/o.conf": "[Service]\nEnvironment=PACK=1\n"})
        assert ctx.r.returncode == 2, ctx.both
        assert "drop-in" not in ctx.err, ctx.err
        assert str(ctx.unit_dir / MAIN) in ctx.err

    def test_positive_control_forum_home_in_main_unit_with_other_dropins(self, tmp_path):
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False,
                       existing={MAIN: _MAIN_BASE.format(env="Environment=FORUM_HOME=/srv/f"),
                                 MAIN + ".d/o.conf": "[Service]\nEnvironment=PACK=1\n"})
        assert ctx.r.returncode == 0, ctx.both
        assert "Forum service dir: /srv/f " in ctx.out, ctx.out

    def test_main_unit_wins_over_a_dropin_that_also_sets_it(self, tmp_path):
        # the refusal is only for "no FORUM_HOME in the main unit"
        ctx = _install(tmp_path, service_dir_arg=False, dry_run=True, prep=False,
                       existing={MAIN: _MAIN_BASE.format(env="Environment=FORUM_HOME=/srv/f"),
                                 MAIN + ".d/h.conf": "[Service]\nEnvironment=FORUM_HOME=/srv/g\n"})
        assert ctx.r.returncode == 0, ctx.both
        assert "Forum service dir: /srv/f " in ctx.out, ctx.out

    def test_the_dropin_detector_is_byte_identical_in_both_scripts(self):
        assert (_extract_fn(INSTALLER, "_forum_home_dropin")
                == _extract_fn(DRIFT, "_forum_home_dropin"))


# ---------------------------------------------------------------------------
# #3590 -- (A) a flag value that is another flag is refused; (B) a --service-dir
# holding ; $ or % is refused at argument parsing, before anything is written.
# Written BEFORE the implementation (contract-first).
# ---------------------------------------------------------------------------

class TestFlagFollowedByFlag3590:

    @pytest.mark.parametrize("exec_line,flag", [
        ("/bin/sh /bin/sh --db --audit /x", "--db"),
        ("/bin/sh /bin/sh --backup-dir --db /x", "--backup-dir"),
        ("/bin/sh /bin/sh --audit --db /x", "--audit"),
        ("/bin/sh /bin/sh --db --backup-dir", "--db"),
    ], ids=["db-audit", "backup-db", "audit-db", "db-then-flag-last"])
    def test_space_form_with_a_flag_as_value_is_refused_naming_the_flag(
            self, tmp_path, exec_line, flag):
        c = _check(tmp_path, _unit_with_exec(exec_line))
        assert not c.passed and c.r.returncode == 2, c.both
        assert flag in c.r.stderr, c.both

    @pytest.mark.parametrize("exec_line,flag", [
        ("/bin/sh /bin/sh --db=-x", "--db"),
        ("/bin/sh /bin/sh --backup-dir=--audit", "--backup-dir"),
        ("/bin/sh /bin/sh --audit=-", "--audit"),
    ], ids=["db", "backup-dir", "audit"])
    def test_equals_form_with_a_value_starting_with_dash_is_refused(
            self, tmp_path, exec_line, flag):
        c = _check(tmp_path, _unit_with_exec(exec_line))
        assert not c.passed and c.r.returncode == 2, c.both
        assert flag in c.r.stderr, c.both

    def test_positive_control_live_unit_shape_passes(self, tmp_path):
        assert _check(tmp_path, _live_unit(tmp_path / "forum", joiner=" \\\n    ")).passed

    def test_positive_control_equals_form_with_absolute_paths_passes(self, tmp_path):
        assert _check(tmp_path, _live_unit(tmp_path / "forum", eq=True)).passed

    def test_positive_control_percent_h_value_passes(self, tmp_path):
        u = _unit_with_exec(f"%h/bin/py %h/tools/s.py --db {tmp_path}/forum/forum.db "
                            "--audit %h/audit.jsonl")
        assert _check(tmp_path, u, home_files=("bin/py", "tools/s.py")).passed


_BAD_CHARS = [";", "$", "%"]
_BAD_IDS = ["semicolon", "dollar", "percent"]


def _assert_nothing_written(ctx, allowed_files=()):
    """The helper pre-creates empty home dirs and any `existing` units; a refusal
    must add no file anywhere, create no service dir, and run no command."""
    assert not ctx.svc.exists(), f"service dir was created: {ctx.svc}"
    files = {rel for rel, kind, _ in _digest(ctx.home) if kind != "dir"}
    assert files == set(allowed_files), files
    assert _log(ctx) == ""


class TestServiceDirCharacters3590:

    @pytest.mark.parametrize("ch", _BAD_CHARS, ids=_BAD_IDS)
    @pytest.mark.parametrize("dry", [False, True], ids=["real", "dry-run"])
    def test_flag_value_with_the_character_is_refused_before_anything_is_written(
            self, tmp_path, ch, dry):
        ctx = _install(tmp_path, prep=False, dry_run=dry, no_start=False,
                       svc_name=f"for{ch}um")
        assert ctx.r.returncode == 2, ctx.both
        assert ch in ctx.err and "--service-dir" in ctx.err, ctx.err
        assert "Forum service dir" not in ctx.out
        _assert_nothing_written(ctx)

    @pytest.mark.parametrize("ch", _BAD_CHARS, ids=_BAD_IDS)
    @pytest.mark.parametrize("dry", [False, True], ids=["real", "dry-run"])
    def test_reused_forum_home_with_the_character_is_refused_naming_the_unit(
            self, tmp_path, ch, dry):
        target = tmp_path / f"for{ch}um"
        ctx = _install(tmp_path, service_dir_arg=False, prep=False, dry_run=dry,
                       no_start=False,
                       existing={MAIN: _MAIN_BASE.format(env=f"Environment=FORUM_HOME={target}")})
        assert ctx.r.returncode == 2, ctx.both
        assert ch in ctx.err and "FORUM_HOME" in ctx.err, ctx.err
        assert "Forum service dir" not in ctx.out
        assert not target.exists()
        files = {rel for rel, kind, _ in _digest(ctx.home) if kind != "dir"}
        assert files == {f".config/systemd/user/{MAIN}"}, files
        assert _log(ctx) == ""

    def test_positive_control_plain_path_renders_and_passes_its_own_check(self, tmp_path):
        ctx = _install(tmp_path)
        assert ctx.r.returncode == 0, ctx.both
        assert "backup unit ExecStart paths all exist" in ctx.out, ctx.out

    @pytest.mark.parametrize("name", ["for-um", "for_um", "for.um", "for+um"])
    def test_other_special_but_allowed_characters_still_install(self, tmp_path, name):
        ctx = _install(tmp_path, svc_name=name)
        assert ctx.r.returncode == 0, ctx.both
        assert "backup unit ExecStart paths all exist" in ctx.out, ctx.out
