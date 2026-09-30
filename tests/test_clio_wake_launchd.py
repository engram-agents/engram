"""Regressions for the macOS single-seat wake mechanism (engram-alpha#3002).

Mirrors tests/test_agent_session_run.py's stubbing style: replace only the
external executables (tmux, agentctl) the scripts shell out to, and assert
argv/log content. No real tmux session, launchd job, or platform process is
started.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import xml.dom.minidom
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MACOS_DIR = REPO_ROOT / "deploy" / "autostart" / "macos"
PLIST = MACOS_DIR / "com.engram.clio-wake.plist.template"
LAUNCH = MACOS_DIR / "clio-wake-launch.sh"
RUN = MACOS_DIR / "clio-wake-run.sh"
WAKE_CHARGE = MACOS_DIR / "wake-charge-clio.md"
CREDENTIAL_PROBE = MACOS_DIR / "credential-probe.sh"
CREDENTIAL_PROBE_PLIST = MACOS_DIR / "com.engram.clio-credential-probe.plist.template"


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".engram" / "logs").mkdir(parents=True)
    return home


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


class TestPlistTemplate:
    def test_is_well_formed_xml(self) -> None:
        # plistlib chokes on the Apple DOCTYPE without a resolvable DTD in
        # some environments; xml.dom.minidom parses it fine and is what
        # actually proves well-formedness without requiring macOS's plutil.
        xml.dom.minidom.parse(str(PLIST))

    def test_uses_start_interval_not_calendar_interval(self) -> None:
        text = PLIST.read_text(encoding="utf-8")
        assert "<key>StartInterval</key>" in text
        assert "<integer>7200</integer>" in text
        # Check the actual <key> tag, not prose -- the plist's own explanatory
        # comment names "StartCalendarInterval" by way of saying it's NOT used,
        # which a bare substring check would misread as the key being present.
        assert "<key>StartCalendarInterval</key>" not in text

    def test_run_at_load_is_false(self) -> None:
        text = PLIST.read_text(encoding="utf-8")
        idx = text.index("<key>RunAtLoad</key>")
        tail = text[idx : idx + 80]
        assert "<false/>" in tail

    def test_log_paths_use_home_placeholder_not_a_hardcoded_seat_path(self) -> None:
        # S2 (Aleph's round-1 catch): launchd does not expand ~ or $HOME, so
        # a hardcoded /Users/<this-seat> path would silently break on any
        # other install -- the template must use the same placeholder
        # convention as the launch-script path, not a real path.
        text = PLIST.read_text(encoding="utf-8")
        assert "__HOME__" in text
        assert "/Users/engram-sonnet" not in text


class TestLaunchQuietSkip:
    """The duplicate-launch check must be a quiet exit-0 skip, not an error
    (deliberate difference from agent-session-launch.sh — see that script's
    own header comment for why an already-live session is the EXPECTED
    common case at this cadence, not an anomaly). Seat-wide (any `claude`
    process for this user), NOT scoped to this launcher's own tmux session —
    see the launcher's own header comment for why `pgrep` is avoided here."""

    def _run_launch(
        self,
        tmp_path: Path,
        *,
        claude_process_live: bool,
        pre_existing_loop_marker: bool = False,
        ps_stub_body: str | None = None,
        tmux_has_session_rc: int = 1,
        security_stub_body: str | None = None,
        boot_env_content: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        home = _make_home(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        new_session_marker = tmp_path / "new-session-called"
        loop_marker = home / ".engram" / "loop-mode.json"
        if pre_existing_loop_marker:
            loop_marker.write_text('{"pacer": "scheduleWakeup"}', encoding="utf-8")
        if boot_env_content is not None:
            boot_env_file = home / ".engram" / "boot-env"
            boot_env_file.parent.mkdir(parents=True, exist_ok=True)
            boot_env_file.write_text(boot_env_content, encoding="utf-8")

        _write_stub(
            bin_dir / "ps",
            ps_stub_body if ps_stub_body is not None else f"""echo "{ 'claude' if claude_process_live else 'zsh' }" """,
        )
        _write_stub(
            bin_dir / "tmux",
            f"""
if [[ "${{1:-}}" == has-session ]]; then exit {tmux_has_session_rc}; fi
if [[ "${{1:-}}" == new-session ]]; then
  printf '%s\\0' "$@" > "{new_session_marker}"
  exit 0
fi
echo "unexpected tmux invocation: $*" >&2
exit 99
""",
        )
        _write_stub(
            bin_dir / "claude",
            "exit 0",
        )
        _write_stub(
            bin_dir / "agentctl",
            """
if [[ "${1:-}" == session && -z "${2:-}" ]]; then
  echo "Usage: agentctl session <agent> [...] [--charge-file <file>] [...]"
  exit 2
fi
exit 0
""",
        )
        # Default: credential check succeeds, so the pre-existing tests in
        # this class (which predate the credential check entirely) never
        # depend on this host's real Keychain state for
        # "Claude Code-credentials" -- without this stub, once
        # clio-wake-launch.sh gains the credential check, EVERY test here
        # would silently start invoking the REAL `security` binary.
        _write_stub(
            bin_dir / "security",
            security_stub_body
            if security_stub_body is not None
            else """echo "not-the-real-secret" ; exit 0""",
        )

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"

        result = subprocess.run(
            ["/bin/bash", str(LAUNCH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result, new_session_marker, home / ".engram" / "logs" / "clio-wake.log", loop_marker

    def test_skips_quietly_when_a_claude_process_is_already_live(self, tmp_path: Path) -> None:
        result, new_session_marker, log, _ = self._run_launch(tmp_path, claude_process_live=True)
        assert result.returncode == 0, result.stderr
        assert not new_session_marker.exists(), "new-session must not be called when a claude process is already live"
        assert log.exists()
        assert "skip:" in log.read_text(encoding="utf-8")

    def test_proceeds_to_new_session_when_no_claude_process_live(self, tmp_path: Path) -> None:
        result, new_session_marker, log, _ = self._run_launch(tmp_path, claude_process_live=False)
        assert result.returncode == 0, result.stderr
        assert new_session_marker.exists(), "new-session must be called when no claude process is live"
        argv = new_session_marker.read_bytes().decode("utf-8").split("\0")
        assert "new-session" in argv
        assert "clio-wake" in argv
        assert "-s" in argv
        # skip log line must NOT appear on the cold-launch path
        if log.exists():
            assert "skip:" not in log.read_text(encoding="utf-8")

    def test_a_session_outside_this_launchers_own_tmux_still_triggers_skip(self, tmp_path: Path) -> None:
        """The exact bug Aleph's round-1 review named: an interactive
        session not started by this launcher (so no `clio-wake` tmux
        session exists) must still be treated as live. Simulated here by
        `claude_process_live=True` with no tmux-session concept involved at
        all in the stub — the check must not depend on tmux."""
        result, new_session_marker, log, _ = self._run_launch(tmp_path, claude_process_live=True)
        assert result.returncode == 0, result.stderr
        assert not new_session_marker.exists(), "an interactive (non-tmux) claude session must also prevent a second launch"

    def test_no_sigpipe_race_when_claude_is_first_in_ps_output(self, tmp_path: Path) -> None:
        """B5 (Aleph's round-2 catch): `ps | grep -qx claude` under this
        script's own `pipefail` reports a FALSE not-found roughly half the
        time when the match is early in a large `ps` listing, because
        `grep -q` exits the instant it matches, `ps` gets SIGPIPE writing
        into the now-closed pipe, and pipefail surfaces THAT exit code
        instead of grep's successful one. Reproduced directly (own stress
        test, not just trusting the report): 25/50 false negatives under
        the old `grep -qx` implementation with 5000 lines behind the match.
        This test pins the fix (awk, which always reads its full input) by
        running the real script many times against exactly that shape."""
        ps_stub = (
            "{ echo claude; "
            "for i in $(seq 1 3000); do echo unrelated-process-$i; done; }"
        )
        for i in range(20):
            result, new_session_marker, _, _ = self._run_launch(
                tmp_path / f"run-{i}", claude_process_live=True, ps_stub_body=ps_stub
            )
            assert result.returncode == 0, result.stderr
            assert not new_session_marker.exists(), (
                "a live claude process at the front of a large ps listing "
                "must never be misread as absent"
            )

    def test_recognizes_claude_reported_as_a_full_path(self, tmp_path: Path) -> None:
        """S-item (Aleph's round-2 catch): some `ps` implementations/configs
        report `comm` as a full path (e.g. /usr/local/bin/claude) rather
        than a bare name. The check must still recognize it as live."""
        result, new_session_marker, _, _ = self._run_launch(
            tmp_path, claude_process_live=True, ps_stub_body='echo "/usr/local/bin/claude"'
        )
        assert result.returncode == 0, result.stderr
        assert not new_session_marker.exists(), "a full-path comm value must still be recognized as a live claude process"

    def test_heartbeat_touched_on_skip_path(self, tmp_path: Path) -> None:
        result, _, _, _ = self._run_launch(tmp_path, claude_process_live=True)
        home = tmp_path / "home"
        assert result.returncode == 0, result.stderr
        assert (home / ".engram" / "wake-checker-heartbeat").exists(), "S1: heartbeat must be touched even on the skip path"

    def test_heartbeat_touched_on_cold_launch_path(self, tmp_path: Path) -> None:
        result, _, _, _ = self._run_launch(tmp_path, claude_process_live=False)
        home = tmp_path / "home"
        assert result.returncode == 0, result.stderr
        assert (home / ".engram" / "wake-checker-heartbeat").exists(), "S1: heartbeat must be touched on the cold-launch path too"


class TestTmuxCollisionGuard:
    """Round-4 catch (maintainer-seat reviewer-fairy): the seat-wide `ps`
    liveness check proves no claude PROCESS is live -- it says nothing about
    whether a `clio-wake`-named tmux session still exists from an earlier
    launch's held failure pane (clio-wake-run.sh's own designed
    pane-hold-on-failure behavior). Reaching the cold-launch path with that
    session still present must refuse loudly, never blindly retry into a
    `tmux new-session` name collision that would otherwise wedge every
    later tick silently."""

    def test_refuses_when_a_stale_tmux_session_still_exists(self, tmp_path: Path) -> None:
        result, new_session_marker, log, _ = TestLaunchQuietSkip()._run_launch(
            tmp_path, claude_process_live=False, tmux_has_session_rc=0
        )
        assert result.returncode != 0, "must fail loudly, not silently succeed"
        assert not new_session_marker.exists(), "must never attempt tmux new-session into a name collision"
        assert log.exists()
        log_text = log.read_text(encoding="utf-8")
        assert "already exists" in log_text
        assert "tmux attach -t clio-wake" in log_text

    def test_proceeds_normally_when_no_stale_session_exists(self, tmp_path: Path) -> None:
        result, new_session_marker, _, _ = TestLaunchQuietSkip()._run_launch(
            tmp_path, claude_process_live=False, tmux_has_session_rc=1
        )
        assert result.returncode == 0, result.stderr
        assert new_session_marker.exists()


class TestStaleLoopMarkerHandling:
    """B3 (req 4): a surviving loop-mode.json must be moved aside on the
    cold-launch path, ONLY once no live claude process could be relying on
    it — otherwise the harness's own loop-arm Stop hook would force-override
    this charge's 'hold, don't resume' instruction every turn."""

    def test_stale_marker_moved_aside_when_no_claude_process_live(self, tmp_path: Path) -> None:
        result, _, log, loop_marker = TestLaunchQuietSkip()._run_launch(
            tmp_path, claude_process_live=False, pre_existing_loop_marker=True
        )
        assert result.returncode == 0, result.stderr
        assert not loop_marker.exists(), "the original loop-mode.json must no longer be at its original path"
        stale_siblings = list(loop_marker.parent.glob("loop-mode.json.stale-*"))
        assert len(stale_siblings) == 1, f"expected exactly one moved-aside marker, found {stale_siblings}"
        assert "moved stale loop marker aside" in log.read_text(encoding="utf-8")

    def test_marker_left_untouched_when_a_claude_process_is_live(self, tmp_path: Path) -> None:
        """The dangerous direction: if a live (non-tmux-visible) session
        legitimately owns this marker, the launcher must never touch it —
        it can't distinguish 'stale' from 'live but invisible to tmux' any
        other way than the liveness check gating this step entirely."""
        result, _, _, loop_marker = TestLaunchQuietSkip()._run_launch(
            tmp_path, claude_process_live=True, pre_existing_loop_marker=True
        )
        assert result.returncode == 0, result.stderr
        assert loop_marker.exists(), "a marker must be left alone when a claude process is live (skip path never reaches the marker-handling code)"
        assert loop_marker.read_text(encoding="utf-8") == '{"pacer": "scheduleWakeup"}'

    def test_no_marker_present_is_a_silent_no_op(self, tmp_path: Path) -> None:
        result, _, log, loop_marker = TestLaunchQuietSkip()._run_launch(
            tmp_path, claude_process_live=False, pre_existing_loop_marker=False
        )
        assert result.returncode == 0, result.stderr
        assert not loop_marker.exists()
        assert list(loop_marker.parent.glob("loop-mode.json.stale-*")) == []
        if log.exists():
            assert "moved stale loop marker aside" not in log.read_text(encoding="utf-8")


class TestRunPaneHoldOnFailure:
    def test_holds_pane_and_logs_on_agentctl_failure(self, tmp_path: Path) -> None:
        home = _make_home(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        charge_dir = tmp_path / "charge"
        charge_dir.mkdir()
        (charge_dir / "wake-charge-clio.md").write_text("test charge content, mentions engram-alpha#3002", encoding="utf-8")
        cwd = tmp_path / "project"
        cwd.mkdir()

        _write_stub(bin_dir / "agentctl", "exit 7")

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"

        proc = subprocess.Popen(
            ["/bin/bash", str(RUN), str(charge_dir), str(cwd)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # The script must hold the pane (sleep infinity) rather than
            # exiting — give it a moment then confirm it is STILL ALIVE,
            # not that it eventually exits.
            time.sleep(2)
            assert proc.poll() is None, "process must still be holding the pane, not exited"
        finally:
            proc.kill()
            proc.wait(timeout=5)

        log = home / ".engram" / "logs" / "clio-wake.log"
        assert log.exists()
        log_text = log.read_text(encoding="utf-8")
        assert "launching:" in log_text
        assert "exited ec=7" in log_text


class TestWakeCharge:
    # Mirrors tools/release_gates.py's own no-new-shipped-node-ids pattern
    # exactly (B1, Aleph's round-1 catch: this file cited a concrete lesson
    # ID from this seat's own graph, which renumbers per-install and so
    # resolves to nothing, or something else, on every other seat).
    _NODE_ID_RE = re.compile(r"\b[a-z]{2}_[0-9]{4,}\b")

    def test_non_empty_and_mentions_the_issue(self) -> None:
        text = WAKE_CHARGE.read_text(encoding="utf-8")
        assert len(text.strip()) > 0
        assert "3002" in text

    def test_does_not_instruct_auto_resuming_the_loop(self) -> None:
        text = WAKE_CHARGE.read_text(encoding="utf-8")
        # The charge must explicitly hold rather than self-authorize
        # re-arming the standing loop -- a plain substring check on the
        # governing instruction, not exhaustive prose-parsing.
        assert "stop and hold" in text.lower() or "hold for" in text.lower()

    def test_cites_no_concrete_engram_node_ids(self) -> None:
        text = WAKE_CHARGE.read_text(encoding="utf-8")
        matches = self._NODE_ID_RE.findall(text)
        assert matches == [], f"found node-ID-shaped citations that will not resolve on another seat: {matches}"

    def test_measures_gap_from_transcript_mtime_not_session_start_time(self) -> None:
        # S3 (Aleph's round-2 catch): a long-running other session's
        # started_at understates how recently it was actually active --
        # the fix reads that session's transcript_path and uses ITS mtime.
        text = WAKE_CHARGE.read_text(encoding="utf-8")
        assert "transcript_path" in text
        assert "getmtime" in text

    @staticmethod
    def _extract_bash_block() -> str:
        text = WAKE_CHARGE.read_text(encoding="utf-8")
        start = text.index("```bash\n") + len("```bash\n")
        end = text.index("\n```", start)
        return text[start:end]

    def test_gap_snippet_skips_a_dev_null_transcript_not_stuck_on_it(self, tmp_path: Path) -> None:
        """B6 (Aleph's round-3 catch): a probe/synthetic session marker
        whose transcript_path is /dev/null passes `[ -e ]` (a device node
        genuinely exists) but must NOT be accepted as a real transcript --
        this test runs the ACTUAL extracted snippet, not a substring check,
        against a synthetic /dev/null marker plus a real one, and confirms
        the real one wins.

        PLATFORM NOTE (maintainer-seat fairy, round 4): this control does
        NOT discriminate on Linux -- reverting the fix (-f -> -e) left this
        test green there too, because /dev/null's mtime on that host is a
        static past timestamp, so the freshly-written real transcript always
        wins the newest-comparison regardless of whether /dev/null was
        excluded correctly. On macOS /dev/null's mtime tracks "now", which
        is why the bug and the fix are both real there (confirmed live by
        Aleph). This is portability of the CONTROL, not of the fix itself --
        the underlying -e vs -f correctness holds on both platforms; only
        this particular test's ability to detect a reverted fix is
        host-dependent."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # The "probe" marker: transcript_path is /dev/null.
        (sessions_dir / "probe-session.json").write_text(
            '{"transcript_path": "/dev/null"}', encoding="utf-8"
        )

        # A real, older transcript for a genuine other session.
        real_transcript = tmp_path / "real-transcript.jsonl"
        real_transcript.write_text("{}\n", encoding="utf-8")
        (sessions_dir / "real-session.json").write_text(
            f'{{"transcript_path": "{real_transcript}"}}', encoding="utf-8"
        )

        # The current session's own marker (must be excluded).
        (sessions_dir / "current-session.json").write_text(
            '{"transcript_path": "/dev/null"}', encoding="utf-8"
        )

        script = self._extract_bash_block().replace(
            'CURRENT_SESSION_ID="<fill in from your own SessionStart banner>"',
            'CURRENT_SESSION_ID="current-session"',
        ).replace(
            'SESSIONS_DIR="$HOME/.engram/sessions"',
            f'SESSIONS_DIR="{sessions_dir}"',
        )

        result = subprocess.run(
            ["/bin/bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        # Must have found the REAL transcript, not reported "unknown" (which
        # is what happens if -f correctly rejects /dev/null but nothing
        # else is found) and not silently accepted /dev/null as valid.
        assert "gap_seconds=" in result.stdout, result.stdout
        assert str(real_transcript) in result.stdout, result.stdout
        assert "/dev/null" not in result.stdout


class TestCredentialProbe:
    """credential-probe.sh (engram-alpha#3002 follow-on) -- a throwaway,
    one-time diagnostic, NOT part of the real wake chain. It answers whether
    a LaunchDaemon-started process (no GUI session in its ancestry) can read
    this account's login Keychain. See this directory's README.md "Before
    installing" section and the script's own header for the full context.

    These tests stub `security` exactly like the existing TestLaunchQuietSkip
    class stubs `ps`/`tmux`/`agentctl` -- a fake binary on a fake PATH, the
    real script run via subprocess, assertions on exit code + file content.
    """

    _SECRET_MARKER = "PASSWORD_VALUE_SECRET_XYZ"

    def _run_probe(
        self, tmp_path: Path, *, security_stub_body: str
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        home = _make_home(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_stub(bin_dir / "security", security_stub_body)

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"

        result = subprocess.run(
            ["/bin/bash", str(CREDENTIAL_PROBE)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result, home / ".engram" / "credential-probe-result.txt"

    def test_success_writes_result_without_leaking_the_secret(self, tmp_path: Path) -> None:
        # `security -w` on a genuinely unlocked keychain would print the
        # secret to STDOUT -- this stub reproduces that shape so the test can
        # prove the redirect-to-/dev/null actually works, not just assert it
        # looks fine.
        result, result_file = self._run_probe(
            tmp_path,
            security_stub_body=f'echo "{self._SECRET_MARKER}"; exit 0',
        )
        assert result.returncode == 0, result.stderr
        assert result_file.exists()
        text = result_file.read_text(encoding="utf-8")
        assert self._SECRET_MARKER not in text, "the secret value must never reach the result file"
        # timestamp + success indication, single line
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)
        assert "success" in text.lower()
        assert len(text.strip().splitlines()) == 1, f"result file must be single-line: {text!r}"

    def test_failure_records_exit_code_and_stderr_and_propagates_exit_code(self, tmp_path: Path) -> None:
        stderr_text = "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."
        result, result_file = self._run_probe(
            tmp_path,
            security_stub_body=f'echo "{stderr_text}" >&2; exit 44',
        )
        assert result.returncode == 44, result.stderr
        assert result_file.exists()
        text = result_file.read_text(encoding="utf-8")
        assert stderr_text in text, text
        assert "44" in text, text
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)
        assert len(text.strip().splitlines()) == 1, f"result file must be single-line: {text!r}"


class TestCredentialProbePlistTemplate:
    def test_is_well_formed_xml(self) -> None:
        xml.dom.minidom.parse(str(CREDENTIAL_PROBE_PLIST))

    def test_run_at_load_is_true(self) -> None:
        text = CREDENTIAL_PROBE_PLIST.read_text(encoding="utf-8")
        idx = text.index("<key>RunAtLoad</key>")
        tail = text[idx : idx + 80]
        assert "<true/>" in tail

    def test_uses_placeholders_not_a_hardcoded_account_or_path(self) -> None:
        text = CREDENTIAL_PROBE_PLIST.read_text(encoding="utf-8")
        assert "__CLIO_ACCOUNT__" in text
        assert "__CREDENTIAL_PROBE_SH__" in text
        assert "__HOME__" in text
        assert "/Users/engram-sonnet" not in text

    def test_username_field_is_the_account_placeholder_not_a_literal_username(self) -> None:
        # The UserName key specifically must carry the placeholder, not a
        # real macOS account name hardcoded anywhere in the repo.
        text = CREDENTIAL_PROBE_PLIST.read_text(encoding="utf-8")
        idx = text.index("<key>UserName</key>")
        tail = text[idx : idx + 120]
        assert "__CLIO_ACCOUNT__" in tail


class TestCredentialCheckFailsLoud:
    """clio-wake-launch.sh's credential-availability check (engram-alpha#3002
    follow-on): runs unconditionally, as the first executable action in the
    script, before the heartbeat touch and before the duplicate-launch skip
    check. On failure it must short-circuit EVERYTHING downstream -- no
    boot-env sourcing artifact, no tmux, no loop-marker handling -- and log
    to the existing $LOG. On success it must be a byte-identical no-op."""

    def test_failure_exits_nonzero_logs_and_short_circuits_everything_downstream(
        self, tmp_path: Path
    ) -> None:
        stderr_text = "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."
        result, new_session_marker, log, loop_marker = TestLaunchQuietSkip()._run_launch(
            tmp_path,
            claude_process_live=False,
            pre_existing_loop_marker=True,
            security_stub_body=f'echo "{stderr_text}" >&2; exit 51',
        )
        assert result.returncode == 51, result.stderr
        assert log.exists()
        log_text = log.read_text(encoding="utf-8")
        assert "51" in log_text
        assert stderr_text in log_text
        # short-circuit proof: none of the downstream side effects happened
        assert not new_session_marker.exists(), "tmux new-session must never be reached on credential failure"
        assert loop_marker.exists(), "the loop marker must be untouched -- the loop-marker handling code must never run"
        assert loop_marker.read_text(encoding="utf-8") == '{"pacer": "scheduleWakeup"}'
        assert not (tmp_path / "home" / ".engram" / "wake-checker-heartbeat").exists(), (
            "the heartbeat touch is downstream of the credential check and must not run either"
        )

    def test_success_is_byte_identical_to_the_pre_existing_quiet_skip_case(self, tmp_path: Path) -> None:
        # Regression guard: re-runs one of TestLaunchQuietSkip's own existing
        # scenarios with an explicit passing `security` stub and asserts the
        # SAME outcome that scenario already asserts -- proving the new
        # check is a true no-op on the happy path, not merely "also passes".
        result, new_session_marker, log, _ = TestLaunchQuietSkip()._run_launch(
            tmp_path, claude_process_live=True, security_stub_body='exit 0'
        )
        assert result.returncode == 0, result.stderr
        assert not new_session_marker.exists(), "new-session must not be called when a claude process is already live"
        assert log.exists()
        log_text = log.read_text(encoding="utf-8")
        assert "skip:" in log_text
        # no new output introduced by the credential check on the happy path
        assert "credential" not in log_text.lower()

    def test_credential_check_gates_before_the_duplicate_launch_skip_check(self, tmp_path: Path) -> None:
        # If the skip check ran FIRST, a live claude process would make the
        # script quietly exit 0 via the skip path even though credentials
        # are broken -- this proves the credential check runs first and
        # truly gates everything else, not merely "usually runs early".
        result, new_session_marker, log, _ = TestLaunchQuietSkip()._run_launch(
            tmp_path,
            claude_process_live=True,  # would trigger the quiet skip path if reached
            security_stub_body='echo "keychain locked" >&2; exit 36',
        )
        assert result.returncode == 36, result.stderr
        assert not new_session_marker.exists()
        log_text = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "skip:" not in log_text, "must exit via the credential-failure path, not the quiet-skip path"
        assert "36" in log_text
        assert "keychain locked" in log_text


class TestPathExportPrependOnlyWhenAbsent:
    """PR-3246 round-1 follow-up (Kepler): a test-only switch in the
    production script means the production PATH-export branch is the one no
    test ever runs. These tests exercise the REAL export line directly,
    under a PATH shaped like launchd's own minimal environment (no
    /opt/homebrew/bin anywhere) -- the one case the prepend is for -- rather
    than relying on the existing tests' incidental PATH contents to prove
    it.

    Root cause this whole class guards against (engram-alpha#3247): the
    script used to unconditionally re-prepend /opt/homebrew/bin ahead of
    whatever PATH the caller supplied, which silently demoted a caller's own
    ordering choice (e.g. a test's stub `bin/` directory standing in for
    tmux) behind the real Homebrew tmux -- on this seat, three tests were
    exercising the real tmux binary instead of their stub without anyone
    noticing.
    """

    def _minimal_launchd_like_path(self, bin_dir: Path) -> str:
        # Deliberately excludes /opt/homebrew/bin and $HOME/.local/bin --
        # the one condition under which the script's prepend must fire.
        # /usr/bin and /bin are real, always-present system dirs so `bash`,
        # `date`, `mkdir` etc. (used by the script itself, before it ever
        # gets to $PATH-dependent tools) keep resolving.
        return f"{bin_dir}:/usr/bin:/bin"

    def _run_to_path_probe(self, home: Path, bin_dir: Path, path: str) -> subprocess.CompletedProcess[str]:
        # Uses CLIO_WAKE_PRINT_PATH_AND_EXIT (see the script's own comment at
        # its definition) rather than letting execution reach tmux at all.
        # SAFETY-CRITICAL, not a style choice: when the prepend under test
        # genuinely fires (this class's whole subject), the real
        # /opt/homebrew/bin/tmux ends up ahead of any stub directory in the
        # resulting PATH -- so an end-to-end run past this point would
        # invoke the REAL tmux and could start a REAL detached session
        # running the REAL runner script. An earlier draft of this test
        # class did exactly that; caught before merge, never before landing
        # in a shipped test. Stopping here is what keeps this test class
        # itself from reproducing the exact hazard engram-alpha#3247 is
        # about.
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = path
        env["CLIO_WAKE_PRINT_PATH_AND_EXIT"] = "1"
        return subprocess.run(
            ["/bin/bash", str(LAUNCH)], env=env, capture_output=True, text=True, timeout=30
        )

    def test_prepends_home_local_bin_and_homebrew_when_neither_is_present(
        self, tmp_path: Path
    ) -> None:
        home = _make_home(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # No tmux/ps/claude/agentctl stubs needed at all: the probe hook
        # exits before any of them would ever be consulted. Only `security`
        # is needed, since the credential check runs before the probe hook.
        _write_stub(bin_dir / "security", 'echo "not-the-real-secret"; exit 0')

        result = self._run_to_path_probe(home, bin_dir, self._minimal_launchd_like_path(bin_dir))
        assert result.returncode == 0, result.stderr

        observed_path = result.stdout.strip()
        expected_prefix = f"{home}/.local/bin:/opt/homebrew/bin:"
        assert observed_path.startswith(expected_prefix), (
            f"expected PATH to start with {expected_prefix!r} after the export, got {observed_path!r}"
        )
        # And the caller's own stub dir must still resolve ahead of any
        # OTHER real system tmux that might exist further down this PATH --
        # i.e. the export prepends, it does not replace, what was there.
        assert str(bin_dir) in observed_path

    def test_does_not_reorder_when_homebrew_bin_already_present(self, tmp_path: Path) -> None:
        # The companion case, proven end-to-end (not just "the 3 pre-existing
        # tests happen to pass"): when /opt/homebrew/bin is ALREADY on PATH
        # (a real interactive shell, or a test's own construction), the
        # export must be a no-op -- the caller's exact ordering survives,
        # letter for letter. Safe to let this one run all the way to the
        # probe hook too, for the same reason as above: no reordering means
        # the caller's own stub dir (wherever the caller put it) keeps
        # whatever position it had, but this test only needs the PATH
        # STRING to prove the no-op, not a live tmux invocation, so it uses
        # the same safe probe.
        home = _make_home(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_stub(bin_dir / "security", 'echo "not-the-real-secret"; exit 0')

        caller_path = f"{bin_dir}:/opt/homebrew/bin:/usr/bin:/bin"
        result = self._run_to_path_probe(home, bin_dir, caller_path)
        assert result.returncode == 0, result.stderr
        observed_path = result.stdout.strip()
        assert observed_path == caller_path, (
            "PATH must be byte-identical to the caller's own when /opt/homebrew/bin "
            f"is already present -- got {observed_path!r}, expected {caller_path!r}"
        )


class TestBootEnvCannotTriggerTestHook:
    """PR-3246 round-2 follow-up (reviewer-fairy catch): the test-only
    introspection hook (CLIO_WAKE_PRINT_PATH_AND_EXIT) must only ever be
    reachable via a value already present in the process's environment
    BEFORE this script started -- never via boot-env, which is sourced with
    `set -a` further down and could re-export this same-named var. An
    operator's leftover debugging line in ~/.engram/boot-env must never turn
    a real launchd wake into a silent no-op that writes nothing to the log
    -- indistinguishable from the dark-seat failure class engram-alpha#3002
    exists to close.

    Uses the SAFE quiet-skip path (claude_process_live=True) so this test
    never risks invoking real tmux -- see TestLaunchQuietSkip's own tests
    for that pattern. Sets CLIO_WAKE_PRINT_PATH_AND_EXIT ONLY via a boot-env
    file, never directly in the test's own env=, so a pass here can only
    mean the production capture-before-sourcing fix is doing its job."""

    def test_boot_env_cannot_trigger_the_print_path_and_exit_hook(self, tmp_path: Path) -> None:
        result, _, log, _ = TestLaunchQuietSkip()._run_launch(
            tmp_path,
            claude_process_live=True,
            boot_env_content="CLIO_WAKE_PRINT_PATH_AND_EXIT=1\n",
        )
        # The hook must NOT have fired and short-circuited: stdout must not
        # be exactly the bare $PATH string the hook would print.
        assert result.stdout.strip() != os.environ["PATH"], (
            "boot-env content must never be able to trigger the test-only "
            "hook -- stdout looks like the hook fired and printed PATH"
        )
        # Execution must have continued past the hook into real script
        # logic, reaching the real quiet-skip behavior.
        assert result.returncode == 0, result.stderr
        assert log.exists()
        assert "skip:" in log.read_text(encoding="utf-8")


class TestTmuxMissingFailsLoud:
    """PR-3246 round-1 follow-up (Kepler): if the PATH export doesn't
    actually make `tmux` resolvable (a wrongly-set switch, a broken
    Homebrew install, anything), the seat must fail loud and dated, not
    silently never start -- the exact dark-seat class engram-alpha#3002 is
    about, one layer inside the wake mechanism itself.

    WHY THIS TESTS THE EXTRACTED SNIPPET, NOT THE FULL SCRIPT (disclosed,
    not silently narrowed): on any seat with a real Homebrew tmux installed
    -- this one included -- "tmux still absent after the PATH export" is
    not constructible via a full end-to-end run. Once the export's prepend
    fires at all, PATH gains the real /opt/homebrew/bin, which genuinely
    has a real tmux on this machine; and if /opt/homebrew/bin is already
    present in the caller's PATH (skipping the prepend, per
    TestPathExportPrependOnlyWhenAbsent), it is the SAME real directory with
    the SAME real tmux. There is no PATH string that makes the literal
    filesystem path /opt/homebrew/bin stop containing a real tmux binary --
    doing that safely would mean moving or hiding the seat's actual
    Homebrew install for the duration of a test, which is a worse hazard
    than the one this test exists to guard against. So this test extracts
    and executes ONLY the two-line guard clause itself (the same technique
    used for PR-3162's #2859 dark-window snippet, for the identical
    reason: the scenario under test cannot be reached by running the whole
    script on real hardware) against a PATH that is genuinely,
    unconditionally tmux-free, proving the CHECK's own logic is correct
    without needing the full script's real-tmux-dependent branches around
    it.
    """

    def test_guard_clause_fails_loud_when_tmux_is_genuinely_absent(self, tmp_path: Path) -> None:
        script_text = LAUNCH.read_text(encoding="utf-8")
        marker = "# FAIL LOUD IF THE EXPORT DIDN'T FIX IT"
        start = script_text.index(marker)
        end = script_text.index("TMUX_SESSION=", start)
        guard_snippet = script_text[start:end]
        assert "command -v tmux" in guard_snippet, "extraction anchor drifted -- update the markers above"

        home = _make_home(tmp_path)
        log_dir = home / ".engram" / "logs"
        log = log_dir / "clio-wake.log"

        # Built from an isolated directory containing ONLY a symlink to the
        # real `date` binary (the one external command the guard snippet's
        # own body calls; `command -v tmux` is a bash builtin and needs no
        # external binary to run) -- tmux-free BY CONSTRUCTION, on any host,
        # rather than by assuming a hardcoded system path lacks a tmux
        # binary. That assumption held on a Mac (Homebrew's tmux lives under
        # /opt/homebrew/bin) but is false on a Linux host with tmux
        # genuinely installed at /usr/bin/tmux (engram-alpha#3246 round-3,
        # Kepler's diagnosis).
        tmux_free_bin = tmp_path / "tmux-free-bin"
        tmux_free_bin.mkdir()
        real_date = shutil.which("date")
        assert real_date is not None, "no `date` binary found on this host -- cannot build the guard's minimal PATH"
        (tmux_free_bin / "date").symlink_to(real_date)

        harness = f"""
set -uo pipefail
LOG="{log}"
{guard_snippet}
echo "REACHED AFTER GUARD"
"""
        result = subprocess.run(
            ["/bin/bash", "-c", harness],
            env={"HOME": str(home), "PATH": str(tmux_free_bin)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, "the guard clause must exit non-zero when tmux is genuinely absent"
        assert "REACHED AFTER GUARD" not in result.stdout, "must exit before the line after the guard"
        assert log.exists()
        assert "tmux not found" in log.read_text(encoding="utf-8").lower()

    def test_guard_clause_is_a_no_op_when_tmux_is_present(self, tmp_path: Path) -> None:
        # Companion/regression case: the same extracted snippet must NOT
        # fire when tmux genuinely resolves (the common case on this seat).
        script_text = LAUNCH.read_text(encoding="utf-8")
        marker = "# FAIL LOUD IF THE EXPORT DIDN'T FIX IT"
        start = script_text.index(marker)
        end = script_text.index("TMUX_SESSION=", start)
        guard_snippet = script_text[start:end]
        assert "command -v tmux" in guard_snippet, "extraction anchor drifted -- update the markers above"

        # Genuinely tmux-present BY CONSTRUCTION, not by whatever happens to
        # be on the ambient PATH (which would make this test's "present"
        # branch host-dependent -- a future CI runner or dev container with
        # no tmux installed would make the guard fire, contradicting this
        # test's own name and assertions, even though it would fail loudly
        # rather than silently pass wrongly). Skip (rather than fabricate a
        # fake tmux) when no real tmux exists on this host at all: this
        # test's whole point is exercising the REAL tmux resolution path.
        real_tmux = shutil.which("tmux")
        if real_tmux is None:
            pytest.skip("no real tmux on this host -- this test needs a real tmux to exercise the 'present' branch")
        real_date = shutil.which("date")
        assert real_date is not None, "no `date` binary found on this host -- cannot build the guard's minimal PATH"

        tmux_present_bin = tmp_path / "tmux-present-bin"
        tmux_present_bin.mkdir()
        (tmux_present_bin / "tmux").symlink_to(real_tmux)
        (tmux_present_bin / "date").symlink_to(real_date)

        home = _make_home(tmp_path)
        log = home / ".engram" / "logs" / "clio-wake.log"
        harness = f"""
set -uo pipefail
LOG="{log}"
{guard_snippet}
echo "REACHED AFTER GUARD"
"""
        result = subprocess.run(
            ["/bin/bash", "-c", harness],
            env={"HOME": str(home), "PATH": str(tmux_present_bin)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "REACHED AFTER GUARD" in result.stdout
        assert not log.exists(), "no log line should be written when tmux is found"
