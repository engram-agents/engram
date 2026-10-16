"""#3597 -- the installer's post-install verify probe must not report the admin
shell's GitHub state as if it were the service's.

Contract-first: written BEFORE the implementation (red on the unchanged base; the
red/green record is in the PR hand-off).

CONTRACT
  S1  ``python -m forum.server --verify-only`` never resolves FORUM_GITHUB_INTEGRATION
      from its own environment (that is the admin shell, not the unit's), so it never
      exits 78 on a switch/token contradiction and never prints a status claim. It
      prints ONE line saying the switch was "not checked" and where the running
      service's own line is (the journalctl command).
  S2  A normal start (no --verify-only) is unchanged: resolve, print, refuse with 78.
  I1  After gate 2 the installer prints the running service's own startup line, read
      from its journal since THIS run's restart, labelled as coming from the running
      service (``tail -1``: the newest line wins).
  I2  No journalctl, a journalctl that fails, or no matching line -> one pointer line
      instead. The install NEVER fails on this (rc 0).
  I3  The restart timestamp is captured BEFORE the restart.
  I4  --no-start reads no journal. --dry-run prints the would-run command.

The installer half runs the installer's real ``if [ "$START" -eq 1 ]`` block (cut out
of the script, with the real ``RUN`` wrapper) under ``set -euo pipefail`` against
stubbed systemctl / curl / sleep / date / journalctl: the full installer cannot reach
gate 2 under the shared harness (its rsync / python3 stubs exit 97).
"""

import os
import shutil
import subprocess
import sys

import pytest

from forum import server as server_mod
from forum.tests.test_deploy_shape_3568 import _extract_fn
from forum.tests.test_installer import _INSTALLER, _REPO_ROOT, _install

TOKEN = "fake-token-for-test-" + "0000"
SRC_DIR = _REPO_ROOT / "src"
NOT_CHECKED = "[forum] GitHub integration: not checked by --verify-only"
JOURNAL_CMD = ("journalctl --user -u engram-forum.service -o cat | "
               "grep '^\\[forum\\] GitHub integration:' | tail -1")
ON_LINE = "[forum] GitHub integration: ON (FORUM_GITHUB_INTEGRATION=on and GH_TOKEN present)"
OLD_LINE = "[forum] GitHub integration: OFF (FORUM_GITHUB_INTEGRATION=off)"
FROM_JOURNAL = "==> GitHub integration (from the running service's journal): " + ON_LINE
POINTER = ("==> GitHub integration: see the service's own startup line: "
           "journalctl --user -u engram-forum.service -o cat | "
           "grep '^\\[forum\\] GitHub integration:' | tail -1")
FIXED_TS = "2099-01-02 03:04:05"


# ---------------------------------------------------------------------------
# Server half
# ---------------------------------------------------------------------------

def _server(tmp_path, env, *args, token=False):
    e = {k: v for k, v in os.environ.items()
         if k not in ("GH_TOKEN", "FORUM_GITHUB_INTEGRATION", "FORUM_HOME")}
    e.update(env)
    e["FORUM_NO_EMBEDDINGS"] = "1"
    e["PYTHONPATH"] = os.pathsep.join([str(SRC_DIR)] + [p for p in sys.path if p])
    if token:
        e["GH_TOKEN"] = TOKEN
    return subprocess.run(
        [sys.executable, "-m", "forum.server", "--db", str(tmp_path / "svc" / "forum.db"),
         "--audit", str(tmp_path / "svc" / "audit.jsonl"), *args],
        capture_output=True, text=True, env=e, cwd=str(SRC_DIR))


class TestServerVerifyOnly:
    def test_on_without_token_does_not_refuse_and_says_not_checked(self, tmp_path):
        r = _server(tmp_path, {"FORUM_GITHUB_INTEGRATION": "on"}, "--verify-only")
        assert r.returncode != 78, r.stdout + r.stderr
        assert "refusing to start" not in r.stderr
        assert NOT_CHECKED in r.stderr
        assert JOURNAL_CMD in r.stderr
        assert "will REFUSE" not in r.stderr and "no token" not in r.stderr

    def test_same_env_without_verify_only_still_exits_78_for_real(self, tmp_path):
        """The normal start is unchanged -- a real run, not a dry run."""
        r = _server(tmp_path, {"FORUM_GITHUB_INTEGRATION": "on"})
        assert r.returncode == 78, r.stdout + r.stderr
        assert "refusing to start" in r.stderr and "GH_TOKEN" in r.stderr
        assert NOT_CHECKED not in r.stderr
        assert not (tmp_path / "svc").exists(), "refusal must precede any data-dir write"

    @pytest.mark.parametrize("env,token", [
        ({}, False), ({"FORUM_GITHUB_INTEGRATION": "off"}, False),
        ({"FORUM_GITHUB_INTEGRATION": "off"}, True),
        ({"FORUM_GITHUB_INTEGRATION": "on"}, True), ({}, True),
        ({"FORUM_GITHUB_INTEGRATION": "maybe"}, False),
    ], ids=["unset", "off", "off+token", "on+token", "unset+token", "bad-value"])
    def test_never_a_status_claim(self, tmp_path, env, token):
        r = _server(tmp_path, env, "--verify-only", token=token)
        assert r.returncode != 78, r.stdout + r.stderr
        lines = [ln for ln in r.stderr.splitlines() if "GitHub integration:" in ln]
        assert len(lines) == 1 and lines[0].startswith(NOT_CHECKED), r.stderr
        assert JOURNAL_CMD in lines[0]
        head = lines[0].split("GitHub integration:", 1)[1].split("(", 1)[0]
        for claim in (" ON", " OFF", "REFUSE", "no token"):
            assert claim not in head, lines[0]
        assert TOKEN not in r.stderr + r.stdout

    def test_in_process_main_does_not_resolve_the_switch_under_verify_only(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
        monkeypatch.setenv("FORUM_GITHUB_INTEGRATION", "on")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr(sys, "argv", [
            "forum.server", "--db", str(tmp_path / "f.db"),
            "--audit", str(tmp_path / "a.jsonl"), "--verify-only"])
        monkeypatch.setattr(server_mod, "probe_deps", lambda *a, **k: {
            "db": "ok", "audit_log": "ok", "engram_pkg": "ok", "embeddings": "ok"})
        called = []
        real = server_mod._resolve_github_integration
        monkeypatch.setattr(server_mod, "_resolve_github_integration",
                            lambda *a, **k: called.append(1) or real(*a, **k))
        with pytest.raises(SystemExit) as ei:
            server_mod.main()
        assert ei.value.code == 0
        assert called == []
        assert NOT_CHECKED in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Installer half
# ---------------------------------------------------------------------------

def _start_block():
    out, on = [], False
    for ln in _INSTALLER.read_text().splitlines():
        if ln == 'if [ "$START" -eq 1 ]; then':
            on = True
        if on:
            out.append(ln)
            if ln == "fi":
                break
    assert out and out[-1] == "fi", "could not cut the START block out of the installer"
    return "\n".join(out)


def _run_block(tmp_path, *, journal="line", start=1, dry_run=0):
    """journal: line | two-lines | empty | fail | absent."""
    stub = tmp_path / "stubs"
    stub.mkdir()
    log = tmp_path / "calls.log"
    # Logs "<name> [arg1][arg2]..." -- one bracket per argument, so quoting is visible.
    logit = ('printf "%s " "$0" | sed "s|.*/||" >> "{log}"\n'
             'for a in "$@"; do printf "[%s]" "$a" >> "{log}"; done\n'
             'echo >> "{log}"\n').format(log=log)

    def mk(name, body):
        f = stub / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(0o755)

    mk("systemctl", logit + "exit 0\n")
    mk("curl", logit + "exit 0\n")
    mk("sleep", "exit 0\n")
    mk("date", f'case "$1" in +*) {logit}echo "{FIXED_TS}";; '
                f'*) exec {shutil.which("date")} "$@";; esac\n')
    jbody = {
        "line": f'echo "{ON_LINE}"\n',
        "two-lines": (f'echo "{OLD_LINE}"\necho "unrelated line"\n'
                      f'echo "{ON_LINE}"\necho "later noise"\n'),
        "empty": 'echo "unrelated line"\n',
        "fail": 'echo "boom" >&2\nexit 1\n',
    }
    if journal != "absent":
        mk("journalctl", logit + jbody[journal])
    py = tmp_path / "forum" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    (tmp_path / "forum" / "app").mkdir()
    if journal == "absent":
        tools = tmp_path / "tools"
        tools.mkdir()
        for t in ("grep", "tail", "head", "seq", "cat", "sed"):
            (tools / t).symlink_to(shutil.which(t))
        path = f"{stub}{os.pathsep}{tools}"
    else:
        path = f"{stub}{os.pathsep}{os.environ['PATH']}"
    script = (
        "set -euo pipefail\n"
        f'START={start}\nDRY_RUN={dry_run}\nUNIT="engram-forum.service"\nPORT=5002\n'
        f'SERVICE_DIR="{tmp_path}/forum"\nAPP="$SERVICE_DIR/app"\nVENV="$SERVICE_DIR/.venv"\nPYTHON="{py}"\n'
        + _extract_fn(_INSTALLER, "RUN") + "\n" + _start_block() + '\necho "INSTALL-DONE"\n')
    r = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True,
                       text=True, env=dict(os.environ, PATH=path))
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


class TestInstallerJournalLine:
    def test_prints_the_services_own_line_labelled_from_the_journal(self, tmp_path):
        r, _ = _run_block(tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert FROM_JOURNAL in r.stdout.splitlines()
        assert "no token" not in r.stdout + r.stderr and "will REFUSE" not in r.stdout + r.stderr
        assert "INSTALL-DONE" in r.stdout

    def test_newest_matching_line_wins_tail_1(self, tmp_path):
        r, _ = _run_block(tmp_path, journal="two-lines")
        assert r.returncode == 0, r.stdout + r.stderr
        assert FROM_JOURNAL in r.stdout.splitlines()
        assert OLD_LINE not in r.stdout and "later noise" not in r.stdout

    def test_journal_line_comes_after_gate_2(self, tmp_path):
        r, _ = _run_block(tmp_path)
        assert "verify-only passed" in r.stdout and FROM_JOURNAL in r.stdout
        assert r.stdout.index("verify-only passed") < r.stdout.index(FROM_JOURNAL)

    def test_journalctl_is_asked_for_this_units_lines_since_the_restart(self, tmp_path):
        _, calls = _run_block(tmp_path)
        j = [c for c in calls if c.startswith("journalctl ")]
        assert len(j) == 1, calls
        for want in ("[--user]", "[-u][engram-forum.service]", f"[--since][{FIXED_TS}]",
                     "[-o][cat]"):
            assert want in j[0], j[0]

    def test_restart_timestamp_is_captured_before_the_restart(self, tmp_path):
        _, calls = _run_block(tmp_path)
        idx_date = next(i for i, c in enumerate(calls) if c.startswith("date "))
        idx_restart = next(i for i, c in enumerate(calls)
                           if c.startswith("systemctl ") and "[restart]" in c)
        idx_journal = next(i for i, c in enumerate(calls) if c.startswith("journalctl "))
        assert idx_date < idx_restart < idx_journal, calls


class TestInstallerNeverFailsOnTheJournal:
    @pytest.mark.parametrize("journal", ["empty", "fail", "absent"])
    def test_pointer_line_and_rc_0(self, tmp_path, journal):
        r, calls = _run_block(tmp_path, journal=journal)
        assert r.returncode == 0, r.stdout + r.stderr
        assert POINTER in r.stdout.splitlines(), r.stdout
        assert "from the running service's journal" not in r.stdout
        assert "INSTALL-DONE" in r.stdout
        if journal == "absent":
            assert not any(c.startswith("journalctl ") for c in calls)


class TestStartAndDryRunModes:
    def test_no_start_reads_no_journal(self, tmp_path):
        r, calls = _run_block(tmp_path, start=0)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "--no-start" in r.stdout
        assert not any(c.startswith(("journalctl", "date ")) for c in calls), calls
        assert "GitHub integration" not in r.stdout

    def test_dry_run_block_prints_the_would_run_command_and_calls_nothing(self, tmp_path):
        r, calls = _run_block(tmp_path, dry_run=1)
        assert r.returncode == 0, r.stdout + r.stderr
        line = [ln for ln in r.stdout.splitlines() if "journalctl" in ln]
        assert len(line) == 1 and "[dry-run]" in line[0] and "--since" in line[0], r.stdout
        assert not any(c.startswith("journalctl") for c in calls)

    def test_full_installer_dry_run_mentions_the_journal_read(self, tmp_path):
        r, _, _ = _install(tmp_path, [])
        assert r.returncode == 0, r.stdout + r.stderr
        assert any("[dry-run]" in ln and "journalctl" in ln and "--since" in ln
                   for ln in r.stdout.splitlines()), r.stdout

    def test_full_installer_no_start_dry_run_has_no_journal_line(self, tmp_path):
        r, _, _ = _install(tmp_path, ["--no-start"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "journalctl --user -u engram-forum.service --since" not in r.stdout
