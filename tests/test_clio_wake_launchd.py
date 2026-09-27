"""Regressions for the macOS single-seat wake mechanism (engram-alpha#3002).

Mirrors tests/test_agent_session_run.py's stubbing style: replace only the
external executables (tmux, agentctl) the scripts shell out to, and assert
argv/log content. No real tmux session, launchd job, or platform process is
started.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import xml.dom.minidom
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MACOS_DIR = REPO_ROOT / "deploy" / "autostart" / "macos"
PLIST = MACOS_DIR / "com.engram.clio-wake.plist.template"
LAUNCH = MACOS_DIR / "clio-wake-launch.sh"
RUN = MACOS_DIR / "clio-wake-run.sh"
WAKE_CHARGE = MACOS_DIR / "wake-charge-clio.md"


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
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        home = _make_home(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        new_session_marker = tmp_path / "new-session-called"
        loop_marker = home / ".engram" / "loop-mode.json"
        if pre_existing_loop_marker:
            loop_marker.write_text('{"pacer": "scheduleWakeup"}', encoding="utf-8")

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
