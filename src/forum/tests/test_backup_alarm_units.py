"""Contract tests: systemd alarm wiring for the forum backup (engram-alpha#3276, C9/C10/C12).

Written BEFORE the implementation (contract-first, #3045).

  C9   engram-forum-backup.service has ``OnFailure=engram-forum-backup-alarm.service``.
  C10  the alarm unit exists, is Type=oneshot, has an ExecStart that runs
       ``forum_backup.py --alarm --unit engram-forum-backup.service``, and sets
       ``Environment=FORUM_BACKUP_ALARM_MENTION=`` (seats to @mention).
  C12  install-forum-service.sh INSTALLS the alarm unit alongside the backup unit
       and runs ``systemctl --user daemon-reload``; it does NOT enable it, and the
       alarm unit has no [Install]/WantedBy (a oneshot started only by OnFailure=
       needs no enable; WantedBy would start it at login and post a false alarm).
       A reinstall must not lose it. (Corrected per the designer, 2026-10-01; the
       first draft of this contract said "installs AND enables".)

Facts about the installer on dev (read, not assumed): it has ``--dry-run`` (every
mutating step goes through ``RUN``, which only prints) and today it does NOT
install the backup service/timer at all (that is a manual runbook step in
deploy/README.md). So "alongside the backup unit" is tested as: the dry-run plan
installs the alarm unit file and enables it, in the same plan as the backup
unit. The dry run is hermetic here: HOME and --service-dir point into tmp_path.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))
from forum.coordination.names import is_valid_agent_name  # noqa: E402
DEPLOY = _REPO / "src" / "forum" / "deploy"
# The two units are TEMPLATES ({{FORUM_HOME}}, #3568); the installer renders them.
BACKUP_UNIT = DEPLOY / "engram-forum-backup.service.template"
ALARM_UNIT = DEPLOY / "engram-forum-backup-alarm.service.template"
INSTALLER = DEPLOY / "install-forum-service.sh"


# MARKER LIST, not a roster: names of fleet seats that must never appear in
# shipped deploy files (a shipped default that pages a specific fleet identity
# is functional and misleading on every other install). Extend when a name is
# found; this list is a tripwire, not a record of who exists.
FLEET_NAME_MARKERS = ("ariadne", "kepler", "borges", "luria", "clio", "zephyr", "mira", "aleph", "sol")


def _effective_env(path: Path) -> dict:
    """Environment= the way systemd parses it: each line is split on whitespace
    with shell-like quoting (a double-quoted assignment keeps its spaces, an
    unquoted one splits), quotes removed."""
    env: dict = {}
    for line in _parse_unit(path).get("Service", {}).get("Environment", []):
        for tok in shlex.split(line):
            if "=" in tok:
                k, v = tok.split("=", 1)
                env[k] = v
    return env


def _rendered_copy(template: Path, dest_dir: Path) -> Path:
    """The template rendered for a scratch service dir, under its real unit name
    (systemd-analyze wants a .service file with absolute ExecStart paths)."""
    out = dest_dir / template.name.removesuffix(".template")
    out.write_text(template.read_text(encoding="utf-8").replace("{{FORUM_HOME}}", str(dest_dir / "forum")),
                   encoding="utf-8")
    return out


def _parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """Minimal systemd-unit parser: {section: {key: [values...]}}; joins
    backslash continuations; ignores comments."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\\\n\s*", " ", text)
    out: dict[str, dict[str, list[str]]] = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if "=" in line and section:
            k, v = line.split("=", 1)
            out[section].setdefault(k.strip(), []).append(v.strip())
    return out


# ---------------------------------------------------------------------------
# C9 / C10 -- unit files
# ---------------------------------------------------------------------------

class TestUnitFiles:

    def test_shipped_deploy_files_name_no_fleet_seat(self):
        hits = []
        # Scope: the files the backup/alarm work ships or edits. (forum-drift-check.sh
        # and README.md carry an older, unrelated seat name; out of this PR's scope.)
        scoped = [*DEPLOY.glob("engram-forum-backup*"), INSTALLER,
                  DEPLOY / "engram-forum.service.template"]
        for f in sorted(scoped):
            if f.suffix in (".service", ".timer", ".sh", ".template"):
                for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    for n in FLEET_NAME_MARKERS:
                        if re.search(rf"\b{n}\b", ln, re.I):
                            hits.append(f"{f.name}:{i}: {n}")
        assert not hits, hits

    def test_backup_unit_names_the_alarm_in_onfailure(self):
        u = _parse_unit(BACKUP_UNIT)
        on_failure = " ".join(u["Unit"].get("OnFailure", []))
        assert "engram-forum-backup-alarm.service" in on_failure.split()

    def test_backup_unit_otherwise_unchanged(self):
        """Don't lose what is already there: oneshot + the backup ExecStart."""
        u = _parse_unit(BACKUP_UNIT)
        assert u["Service"]["Type"] == ["oneshot"]
        assert "forum_backup.py" in u["Service"]["ExecStart"][0]
        assert "--backup-dir" in u["Service"]["ExecStart"][0]

    def test_alarm_unit_exists(self):
        assert ALARM_UNIT.is_file(), f"{ALARM_UNIT} missing"

    def test_alarm_unit_is_oneshot_with_execstart(self):
        u = _parse_unit(ALARM_UNIT)
        assert u["Service"]["Type"] == ["oneshot"]
        assert len(u["Service"]["ExecStart"]) == 1

    def test_alarm_execstart_runs_forum_backup_alarm_for_the_backup_unit(self):
        exec_start = _parse_unit(ALARM_UNIT)["Service"]["ExecStart"][0]
        assert "forum_backup.py" in exec_start
        assert "--alarm" in exec_start
        assert re.search(r"--unit\s+engram-forum-backup\.service\b", exec_start)

    def test_alarm_unit_ships_no_mention_default(self):
        """The mention is a per-install value written by the installer drop-in
        (mention.conf); the shipped unit must not decide who gets paged."""
        env_lines = _parse_unit(ALARM_UNIT)["Service"].get("Environment", [])
        assert not any("FORUM_BACKUP_ALARM_MENTION" in e for e in env_lines), env_lines

    def test_alarm_unit_posts_as_a_distinct_valid_agent(self):
        """db.get_mentions excludes self-posts: the default poster is a generic
        agent that no installing admin's name can collide with by construction."""
        agent = _effective_env(ALARM_UNIT).get("FORUM_BACKUP_ALARM_AGENT", "")
        assert agent, "FORUM_BACKUP_ALARM_AGENT must be set in the unit"
        assert is_valid_agent_name(agent), agent

    def test_systemd_analyze_reports_no_invalid_environment_assignment(self, tmp_path):
        exe = shutil.which("systemd-analyze")
        if exe is None:
            pytest.skip("systemd-analyze not installed")
        work = _rendered_copy(ALARM_UNIT, tmp_path)
        # system-mode verify: needs no user bus
        r = subprocess.run([exe, "verify", str(work)], capture_output=True, text=True)
        text = r.stdout + r.stderr
        bad = [ln for ln in text.splitlines() if "Invalid environment assignment" in ln]
        assert not bad, text

    def test_alarm_unit_does_not_itself_chain_onfailure_to_the_backup(self):
        """No OnFailure loop: the alarm failing is what shows in --failed."""
        u = _parse_unit(ALARM_UNIT)
        assert "engram-forum-backup.service" not in " ".join(u.get("Unit", {}).get("OnFailure", []))

    @pytest.mark.parametrize("unit", [BACKUP_UNIT, ALARM_UNIT], ids=lambda p: p.name)
    def test_unit_parses_with_systemd_analyze(self, unit, tmp_path):
        exe = shutil.which("systemd-analyze")
        if exe is None:
            pytest.skip("systemd-analyze not installed")
        if not unit.is_file():
            pytest.fail(f"{unit.name} missing")
        # ExecStart paths (venv python, %h/engram-alpha) do not exist on every
        # host and verify reports that; only SYNTAX problems matter here. Copy to
        # tmp so unit-name resolution of the OnFailure target is also local.
        work = _rendered_copy(unit, tmp_path)
        if unit == BACKUP_UNIT and ALARM_UNIT.is_file():
            _rendered_copy(ALARM_UNIT, tmp_path)
        r = subprocess.run(
            [exe, "--user", "verify", str(work)], capture_output=True, text=True,
            env={**os.environ, "SYSTEMD_LOG_LEVEL": "info"},
        )
        text = (r.stdout + r.stderr)
        # Hosts without a user manager / without the interpreter path: tolerate
        # "not found / not executable" noise, fail on parse errors.
        parse_errors = [
            ln for ln in text.splitlines()
            if re.search(r"(Unknown (key|section|lvalue)|Missing '='|Assignment outside of section|"
                         r"Invalid|Failed to parse|Unit .* is not loaded)", ln)
            and "not found" not in ln and "Executable path" not in ln
        ]
        if "Failed to connect to bus" in text and not parse_errors:
            pytest.skip("systemd-analyze cannot reach a user manager on this host")
        assert not parse_errors, text


# ---------------------------------------------------------------------------
# C12 -- installer installs + enables the alarm unit
# ---------------------------------------------------------------------------

class TestInstallerInstallsAlarm:

    @pytest.fixture
    def dry_run_plan(self, tmp_path) -> str:
        home = tmp_path / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home)}
        r = subprocess.run(
            ["bash", str(INSTALLER), "--src", str(_REPO),
             "--service-dir", str(tmp_path / "forum"), "--group", "agents",
             "--dry-run", "--no-start"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert not any(home.iterdir()), "--dry-run must not write under HOME"
        return r.stdout

    def test_dry_run_installs_the_alarm_unit_file(self, dry_run_plan):
        lines = [ln for ln in dry_run_plan.splitlines()
                 if "engram-forum-backup-alarm.service" in ln]
        assert any(re.search(r"\b(install|cp|copy|sed)\b", ln) and "alarm.service" in ln
                   for ln in lines), (
            "no dry-run line installs/copies the alarm unit file; plan was:\n" + dry_run_plan
        )

    def test_dry_run_reloads_systemd_after_installing_the_alarm_unit(self, dry_run_plan):
        lines = dry_run_plan.splitlines()
        install_idx = [i for i, ln in enumerate(lines)
                       if "engram-forum-backup-alarm.service" in ln
                       and re.search(r"\b(install|cp|copy)\b", ln)]
        reload_idx = [i for i, ln in enumerate(lines)
                      if "systemctl" in ln and "daemon-reload" in ln]
        assert install_idx and reload_idx, dry_run_plan
        assert max(install_idx) < max(reload_idx), (
            "daemon-reload must come after the alarm unit is installed"
        )

    def test_dry_run_does_not_enable_the_alarm_unit(self, dry_run_plan):
        """A oneshot started only by OnFailure= needs no enable; enabling it
        (WantedBy) would start it at login and post a false alarm."""
        enable = [ln for ln in dry_run_plan.splitlines()
                  if "systemctl" in ln and "enable" in ln]
        assert not any("engram-forum-backup-alarm" in ln for ln in enable), (
            "alarm unit must NOT be enabled; enable lines: " + repr(enable)
        )

    def test_alarm_unit_has_no_install_section(self):
        u = _parse_unit(ALARM_UNIT)
        assert "Install" not in u, "alarm unit must have no [Install] section"
        assert not any("WantedBy" in sec for sec in u.values())

    def test_alarm_unit_goes_through_the_same_path_as_the_backup_unit(self, dry_run_plan):
        """Installed together: wherever the backup service is installed in the
        plan, the alarm unit is installed to the SAME unit dir, so a reinstall
        that re-copies one re-copies the other."""
        def dest(unit):
            for ln in dry_run_plan.splitlines():
                if unit in ln and re.search(r"\b(install|cp|copy)\b", ln):
                    m = re.search(r"(/\S*systemd/user)\b", ln)
                    if m:
                        return m.group(1)
            return None
        backup_dest, alarm_dest = dest("engram-forum-backup.service"), dest("engram-forum-backup-alarm.service")
        assert backup_dest, "plan never installs the backup unit (contract: 'alongside the backup unit')"
        assert alarm_dest == backup_dest

    def test_installer_is_idempotent_for_the_alarm_unit(self, tmp_path):
        """Reinstall must not lose it: two dry runs plan the same alarm steps."""
        home = tmp_path / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home)}
        outs = []
        for _ in range(2):
            r = subprocess.run(
                ["bash", str(INSTALLER), "--src", str(_REPO),
                 "--service-dir", str(tmp_path / "forum"), "--group", "agents",
             "--dry-run", "--no-start"],
                capture_output=True, text=True, env=env,
            )
            assert r.returncode == 0
            outs.append([ln for ln in r.stdout.splitlines() if "alarm" in ln])
        assert outs[0] and outs[0] == outs[1]

    def test_dry_run_plans_the_onfailure_drop_in(self, dry_run_plan):
        assert any("engram-forum-backup.service.d" in ln and "alarm.conf" in ln
                   for ln in dry_run_plan.splitlines()), dry_run_plan


@pytest.fixture
def run_block(tmp_path):
    text = INSTALLER.read_text(encoding="utf-8")
    m = re.search(r"^# >>> backup-units\n(.*?)^# <<< backup-units\n", text, re.S | re.M)
    assert m, "installer must delimit the backup-unit block with '# >>> backup-units' / '# <<< backup-units'"
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    script = (
        "set -euo pipefail\n"
        f"SRC={shlex.quote(str(_REPO))}\nUNIT_DIR={shlex.quote(str(unit_dir))}\nDRY_RUN=0\n"
        f"SERVICE_DIR={shlex.quote(str(tmp_path / 'forum'))}\n"
        'RUN() { "$@"; }\n' + m.group(1)
    )

    def _run(env: dict | None = None, check: bool = True):
        """env=None keeps the caller's FORUM_BACKUP_ALARM_MENTION/USER out;
        pass a dict to set exactly those two."""
        base = {k: v for k, v in os.environ.items()
                if k not in ("FORUM_BACKUP_ALARM_MENTION", "USER")}
        base.update(env or {})
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=base)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r

    return unit_dir, _run


class TestInstallerBackupUnitBlockRealExecution:
    """Executes the installer's backup-unit block for real (no systemctl, no
    venv): the block between the ``>>> backup-units`` / ``<<< backup-units``
    marker comments is extracted and run in bash against a scratch unit dir.

    Operators hand-edit the backup unit (checkout path, --packs-dir), so a
    reinstall must NOT overwrite it; it must still gain the alarm via a drop-in.
    """

    def test_fresh_install_lays_down_everything(self, run_block):
        unit_dir, run = run_block
        run()
        for name in ("engram-forum-backup.service", "engram-forum-backup.timer",
                     "engram-forum-backup-alarm.service"):
            assert (unit_dir / name).is_file(), name
        dropin = unit_dir / "engram-forum-backup.service.d" / "alarm.conf"
        assert dropin.read_text().split() == ["[Unit]", "OnFailure=engram-forum-backup-alarm.service"]

    def test_reinstall_keeps_hand_edited_backup_unit_and_still_adds_dropin(self, run_block):
        unit_dir, run = run_block
        run()
        svc = unit_dir / "engram-forum-backup.service"
        audit = f"--audit {unit_dir.parent / 'forum'}/forum-audit.jsonl"  # the fixture's SERVICE_DIR
        assert audit in svc.read_text()
        edited = svc.read_text().replace(audit, audit + " --packs-dir /srv/packs")
        assert "--packs-dir /srv/packs" in edited
        svc.write_text(edited)
        (unit_dir / "engram-forum-backup.timer").write_text("[Timer]\nOnCalendar=hourly\n")
        shutil.rmtree(unit_dir / "engram-forum-backup.service.d")  # drop-in lost -> restored

        run()

        assert svc.read_text() == edited, "hand-edited backup unit was overwritten"
        assert (unit_dir / "engram-forum-backup.timer").read_text() == "[Timer]\nOnCalendar=hourly\n"
        assert (unit_dir / "engram-forum-backup.service.d" / "alarm.conf").is_file()

    def test_alarm_unit_is_always_installed_even_if_modified(self, run_block):
        unit_dir, run = run_block
        run()
        alarm = unit_dir / "engram-forum-backup-alarm.service"
        shipped = alarm.read_text()
        alarm.write_text("stale\n")
        run()
        assert alarm.read_text() == shipped

    def test_legacy_backup_unit_without_onfailure_gains_the_alarm(self, run_block):
        unit_dir, run = run_block
        (unit_dir / "engram-forum-backup.service").write_text(
            "[Service]\nType=oneshot\nExecStart=/x\n"
        )
        run()
        assert (unit_dir / "engram-forum-backup.service").read_text() == (
            "[Service]\nType=oneshot\nExecStart=/x\n"
        )
        dropin = (unit_dir / "engram-forum-backup.service.d" / "alarm.conf").read_text()
        assert "OnFailure=engram-forum-backup-alarm.service" in dropin


class TestInstallerWritesMentionDropIn:
    """The per-install mention lives in engram-forum-backup-alarm.service.d/
    mention.conf, written by the installer (never shipped in the unit)."""

    @staticmethod
    def _mention_conf(unit_dir: Path) -> Path:
        return unit_dir / "engram-forum-backup-alarm.service.d" / "mention.conf"

    @staticmethod
    def _value(unit_dir: Path) -> str:
        conf = TestInstallerWritesMentionDropIn._mention_conf(unit_dir)
        assert conf.read_text().startswith("[Service]\n")
        return _effective_env(conf).get("FORUM_BACKUP_ALARM_MENTION", "<unset>")

    # -- value source: installer env var, else $USER minus 'agent-', else empty --

    def test_env_var_value_is_written_quoted_and_survives_systemd_parsing(self, run_block):
        unit_dir, run = run_block
        run(env={"FORUM_BACKUP_ALARM_MENTION": "admin1 admin2", "USER": "agent-ignored"})
        conf = self._mention_conf(unit_dir)
        assert 'Environment="FORUM_BACKUP_ALARM_MENTION=admin1 admin2"' in conf.read_text()
        assert self._value(unit_dir) == "admin1 admin2"   # spaces preserved (shlex == systemd quoting)

    def test_env_var_comma_form_accepted(self, run_block):
        unit_dir, run = run_block
        run(env={"FORUM_BACKUP_ALARM_MENTION": "a-1,b_2"})
        assert self._value(unit_dir) == "a-1,b_2"

    def test_unset_env_uses_user_minus_agent_prefix(self, run_block):
        unit_dir, run = run_block
        run(env={"USER": "agent-someone"})
        assert self._value(unit_dir) == "someone"

    def test_unset_env_user_without_prefix_is_used_as_is(self, run_block):
        unit_dir, run = run_block
        run(env={"USER": "plainuser"})
        assert self._value(unit_dir) == "plainuser"

    def test_neither_env_nor_user_writes_an_empty_value(self, run_block):
        unit_dir, run = run_block
        run(env={})
        assert self._value(unit_dir) == ""

    def test_dropin_is_rewritten_on_every_run(self, run_block):
        unit_dir, run = run_block
        run(env={"FORUM_BACKUP_ALARM_MENTION": "old"})
        run(env={"FORUM_BACKUP_ALARM_MENTION": "new"})
        assert self._value(unit_dir) == "new"

    # -- validation: the value is written into a unit file --

    @pytest.mark.parametrize(
        "bad",
        ["a\nExecStart=/bin/evil", 'a"b', "a$b", "a;b", "a`b", "a\\b", "a'b", "a#b", "a%b", "a&b", "a|b", "a/b"],
        ids=["newline", "dquote", "dollar", "semicolon", "backtick", "backslash",
             "squote", "hash", "percent", "amp", "pipe", "slash"],
    )
    def test_metacharacter_value_is_refused_and_nothing_is_written(self, run_block, bad):
        unit_dir, run = run_block
        r = run(env={"FORUM_BACKUP_ALARM_MENTION": bad}, check=False)
        assert r.returncode != 0
        assert "FORUM_BACKUP_ALARM_MENTION" in (r.stdout + r.stderr), "error must name the knob"
        assert not self._mention_conf(unit_dir).exists()
        assert not (unit_dir / "engram-forum-backup-alarm.service").exists(), "refuse before writing anything"

    def test_dry_run_prints_the_mention_it_would_write(self, tmp_path):
        env = {**os.environ, "HOME": str(tmp_path), "FORUM_BACKUP_ALARM_MENTION": "admin1 admin2"}
        r = subprocess.run(
            ["bash", str(INSTALLER), "--src", str(_REPO), "--service-dir", str(tmp_path / "forum"),
             "--group", "agents", "--dry-run", "--no-start"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert any("mention.conf" in ln and "admin1 admin2" in ln for ln in r.stdout.splitlines()), r.stdout

    def test_systemd_analyze_accepts_unit_plus_written_dropin(self, run_block):
        exe = shutil.which("systemd-analyze")
        if exe is None:
            pytest.skip("systemd-analyze not installed")
        unit_dir, run = run_block
        run(env={"FORUM_BACKUP_ALARM_MENTION": "admin1 admin2"})
        r = subprocess.run([exe, "verify", str(unit_dir / "engram-forum-backup-alarm.service")],
                           capture_output=True, text=True)
        text = r.stdout + r.stderr
        assert "Invalid environment assignment" not in text, text
