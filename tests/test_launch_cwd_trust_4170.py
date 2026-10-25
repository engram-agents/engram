"""Launcher cwd resolution + trust preflight (engram-alpha #4170).

Problem: a freshly renamed seat has neither ~/.engram/boot-cwd nor ~/umwelt, so
agent-session-launch.sh booted in $HOME; Claude Code then stalled, unseen, on its
"trust this folder?" dialog (~3 h on one seat). Claude Code keeps trust per
absolute path in ~/.claude.json -> projects.<abs path>.hasTrustDialogAccepted.
That file is NOT ours: the launcher only ever READS it.

CONTRACT. The launcher resolves the cwd AT LAUNCH TIME from $HOME. Nothing is
written before a rename; nothing absolute is persisted. Selection (round 3):
  1. an explicit ~/.engram/boot-cwd (normalised) that names an existing
     directory wins, EVEN IF UNTRUSTED (it still launches, with the ERROR);
  2. else the FIRST EXISTING directory among $HOME/engram-alpha,
     $HOME/umwelt, $HOME that is TRUSTED (same read-only check as the
     preflight, one shared function);
  3. else, none trusted: the first existing one (that order) plus the ERROR.
The trust check is unverifiable when CLAUDE_CONFIG_DIR is set or python3 is
missing; then every candidate counts as untrusted for selection, so rule 3
applies (engram-alpha > umwelt > $HOME) with the existing ERROR wording.
L1-L4  each arm is chosen in the right situation (L1b: a bad boot-cwd falls
       through to the next arm, with a WARNING naming the real fallback).
L5     every launch prints ONE stderr line `boot cwd: <dir> (from <arm>)`, where
       arm says WHY: `boot-cwd`, `$HOME/x, first trusted` or `$HOME/x, none
       trusted` (and `$HOME, ...` likewise).
S1-S6  round 3 selection cells (see tests below): S1 only umwelt trusted ->
       umwelt, no ERROR; S2 both trusted -> engram-alpha (order breaks ties);
       S3 neither trusted -> engram-alpha + ERROR; S4 only $HOME trusted ->
       $HOME, no ERROR; S5 untrusted explicit boot-cwd beats trusted umwelt
       (+ERROR); S6 CLAUDE_CONFIG_DIR set -> engram-alpha + that ERROR.
T1-T4  trust preflight reads $HOME/.claude.json read-only via `python3 -c`
       json. When the chosen cwd is NOT trusted it prints a LOUD stderr line
       starting `ERROR:` naming the directory and the one-time fix (attach to
       tmux session `agent-<name>` once and accept the dialog). Not trusted:
       T1 key is false; T2 file absent; T3 projects entry/key absent;
       T4 file unparseable. ABSENT OR UNREADABLE READS AS UNTRUSTED.
T5     trusted (true) -> no ERROR line.
T5b    python3 not on PATH -> untrusted, with its own ERROR wording.
T6     the preflight never writes or creates ~/.claude.json (byte-identical /
       still absent afterwards).
T7     on untrusted the launcher STILL launches (loud warning, not a refusal);
       exit status unchanged (0).
T8     CLAUDE_CONFIG_DIR set and non-empty (after boot-env is sourced): Claude
       Code may keep its config elsewhere, so $HOME/.claude.json proves nothing.
       The preflight FAILS CLOSED: ERROR naming the dir and the one-time fix,
       even when $HOME/.claude.json says trusted. It never reads
       $CLAUDE_CONFIG_DIR/.claude.json (layout unverified; guessing = silent pass).
T9/T10 pins (already green before round 2): trust requires `is True`, so
       `1` and the string "true" both read as untrusted.
L6-L8  the first line of boot-cwd is normalised before the -d test: trailing
       CR stripped (L6), leading/trailing whitespace stripped (L7), ONE
       trailing `/` stripped unless the value is just `/` (L8). The preflight
       compares the normalised path. Relative paths are NOT made absolute.

Harness: the real script, copied to tmp with only its RUNNER path rewritten
(same approach as test_boot_env_common.py); stub tmux/agentctl/claude on PATH.
The tmux stub records argv one word per line; the cwd is the last word.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "autostart" / "agent-session-launch.sh"
RUNNER_LINE = 'RUNNER="/usr/local/lib/agentctl/autostart/agent-session-run.sh"'


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _launch(tmp_path: Path, *, dirs=(), boot_cwd=None, claude_json=None, no_python=False,
            extra_env=None, boot_cwd_raw=None):
    """claude_json: None (absent), str (raw content) or dict (json-dumped)."""
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    for d in dirs:
        (home / d).mkdir(parents=True, exist_ok=True)
    if boot_cwd is not None:
        (home / ".engram" / "boot-cwd").write_text(boot_cwd.replace("@HOME@", str(home)) + "\n",
                                                   encoding="utf-8")
    if boot_cwd_raw is not None:
        (home / ".engram" / "boot-cwd").write_bytes(
            boot_cwd_raw.replace("@HOME@", str(home)).encode("utf-8"))
    cj = home / ".claude.json"
    if claude_json is not None:
        cj.write_text(claude_json if isinstance(claude_json, str)
                      else json.dumps(claude_json).replace("@HOME@", str(home)), encoding="utf-8")
    before = cj.read_bytes() if cj.exists() else None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "tmux-argv.txt"
    runner = tmp_path / "runner.sh"
    _stub(runner, "exit 0")
    script = tmp_path / "agent-session-launch.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8").replace(RUNNER_LINE, f'RUNNER="{runner}"'),
                      encoding="utf-8")
    _stub(bin_dir / "claude", "exit 0")
    _stub(bin_dir / "agentctl", 'echo "Usage: agentctl session <a> [--charge-file <f>]"; exit 2')
    _stub(bin_dir / "tmux",
          f'if [[ "${{1:-}}" == has-session ]]; then exit 1; fi\nprintf "%s\\n" "$@" > "{argv_log}"\nexit 0')
    if no_python:
        # PATH holding only what the script needs, deliberately without python3.
        for tool in ("head", "grep", "cat", "dirname"):
            os.symlink(shutil.which(tool), bin_dir / tool)
        path = str(bin_dir)
    else:
        path = f"{bin_dir}:/usr/bin:/bin"
    env = {"HOME": str(home), "PATH": path,
           "AGENT_BOOT_ENV_COMMON": str(tmp_path / "none")}
    env.update({k: v.replace("@HOME@", str(home)) for k, v in (extra_env or {}).items()})
    proc = subprocess.run(["/bin/bash", str(script), "zed"], env=env,
                          capture_output=True, text=True, timeout=30)
    argv = argv_log.read_text(encoding="utf-8").splitlines() if argv_log.exists() else None
    after = cj.read_bytes() if cj.exists() else None
    return proc, home, argv, before, after


def _trusted(value=True):
    return {"projects": {"@HOME@/engram-alpha": {"hasTrustDialogAccepted": value}}}


def _errors(proc):
    return [ln for ln in proc.stderr.splitlines() if ln.startswith("ERROR:")]


def _boot_lines(proc):
    return [ln for ln in proc.stderr.splitlines() if ln.startswith("boot cwd:")]


# ---- L1-L4, L5 -------------------------------------------------------------

def test_L1_boot_cwd_wins(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("custom", "engram-alpha", "umwelt"),
                                   boot_cwd="@HOME@/custom")
    assert proc.returncode == 0, proc.stderr
    assert argv[-1] == str(home / "custom")
    assert _boot_lines(proc) == [f"boot cwd: {home}/custom (from boot-cwd)"]


def test_L1b_bad_boot_cwd_falls_through_with_warning(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "umwelt"),
                                   boot_cwd="@HOME@/nope")
    assert proc.returncode == 0, proc.stderr
    assert argv[-1] == str(home / "engram-alpha")
    assert (f"WARNING: boot-cwd '{home}/nope' is not a directory; "
            f"falling back to '{home}/engram-alpha'") in proc.stderr


def test_L2_engram_alpha_beats_umwelt(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "umwelt"))
    assert argv[-1] == str(home / "engram-alpha")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram-alpha (from $HOME/engram-alpha, none trusted)"]


def test_L3_umwelt(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("umwelt",))
    assert argv[-1] == str(home / "umwelt")
    assert _boot_lines(proc) == [f"boot cwd: {home}/umwelt (from $HOME/umwelt, none trusted)"]


def test_L4_home(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path)
    assert argv[-1] == str(home)
    assert _boot_lines(proc) == [f"boot cwd: {home} (from $HOME, none trusted)"]


# ---- T1-T7 -----------------------------------------------------------------

def _assert_untrusted(proc, cwd):
    errs = _errors(proc)
    assert len(errs) == 1, proc.stderr
    assert str(cwd) in errs[0]
    assert "tmux attach -t agent-zed" in errs[0]
    assert "accept" in errs[0]


def test_T1_key_false(tmp_path):
    proc, home, argv, before, after = _launch(
        tmp_path, dirs=("engram-alpha",), claude_json=_trusted(value=False))
    _assert_untrusted(proc, home / "engram-alpha")
    assert proc.returncode == 0 and argv is not None  # T7
    assert before == after  # T6


def test_T2_file_absent(tmp_path):
    proc, home, argv, before, after = _launch(tmp_path, dirs=("engram-alpha",))
    _assert_untrusted(proc, home / "engram-alpha")
    assert proc.returncode == 0 and argv is not None  # T7
    assert before is None and after is None  # T6: never created


def test_T3_entry_or_key_absent(tmp_path):
    for i, cj in enumerate(({"projects": {}},
                            {"projects": {"@HOME@/engram-alpha": {}}},
                            {"other": 1})):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        proc, home, argv, before, after = _launch(sub, dirs=("engram-alpha",), claude_json=cj)
        _assert_untrusted(proc, home / "engram-alpha")
        assert proc.returncode == 0 and argv is not None
        assert before == after


def test_T4_unparseable(tmp_path):
    proc, home, argv, before, after = _launch(
        tmp_path, dirs=("engram-alpha",), claude_json="{not json")
    _assert_untrusted(proc, home / "engram-alpha")
    assert proc.returncode == 0 and argv is not None
    assert before == after


def test_T5_trusted_no_error(tmp_path):
    proc, home, argv, before, after = _launch(
        tmp_path, dirs=("engram-alpha",), claude_json=_trusted())
    assert proc.returncode == 0, proc.stderr
    assert _errors(proc) == []
    assert argv[-1] == str(home / "engram-alpha")
    assert before == after


def test_T5b_no_python3_is_untrusted_with_own_wording(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha",),
                                   claude_json=_trusted(), no_python=True)
    errs = _errors(proc)
    assert len(errs) == 1, proc.stderr
    assert "python3" in errs[0]
    assert proc.returncode == 0 and argv is not None  # T7


# ---- round 2: T8-T10, L6-L8 -----------------------------------------------

def test_T8_claude_config_dir_fails_closed(tmp_path):
    proc, home, argv, before, after = _launch(
        tmp_path, dirs=("engram-alpha", "cfg"), claude_json=_trusted(),
        extra_env={"CLAUDE_CONFIG_DIR": "@HOME@/cfg"})
    errs = _errors(proc)
    assert len(errs) == 1, proc.stderr
    assert "CLAUDE_CONFIG_DIR" in errs[0]
    assert f"{home}/cfg" in errs[0]
    assert "tmux attach -t agent-zed" in errs[0]
    assert "accept" in errs[0]
    assert proc.returncode == 0 and argv is not None
    assert before == after


def test_T8b_empty_claude_config_dir_is_ignored(tmp_path):
    proc, home, argv, *_ = _launch(
        tmp_path, dirs=("engram-alpha",), claude_json=_trusted(),
        extra_env={"CLAUDE_CONFIG_DIR": ""})
    assert _errors(proc) == [], proc.stderr


def test_T9_truthy_int_is_untrusted(tmp_path):  # pin: already green pre-round-2
    proc, home, *_ = _launch(tmp_path, dirs=("engram-alpha",), claude_json=_trusted(value=1))
    _assert_untrusted(proc, home / "engram-alpha")


def test_T10_string_true_is_untrusted(tmp_path):  # pin: already green pre-round-2
    proc, home, *_ = _launch(tmp_path, dirs=("engram-alpha",), claude_json=_trusted(value="true"))
    _assert_untrusted(proc, home / "engram-alpha")


def test_L6_crlf_boot_cwd(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("custom", "engram-alpha"),
                                   boot_cwd_raw="@HOME@/custom\r\n")
    assert argv[-1] == str(home / "custom")
    assert _boot_lines(proc) == [f"boot cwd: {home}/custom (from boot-cwd)"]


def test_L7_whitespace_boot_cwd(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("custom", "engram-alpha"),
                                   boot_cwd_raw="  @HOME@/custom \t \n")
    assert argv[-1] == str(home / "custom")
    assert _boot_lines(proc) == [f"boot cwd: {home}/custom (from boot-cwd)"]


def test_L8_trailing_slash_boot_cwd_and_preflight_uses_stripped(tmp_path):
    cj = {"projects": {"@HOME@/custom": {"hasTrustDialogAccepted": True}}}
    proc, home, argv, *_ = _launch(tmp_path, dirs=("custom", "engram-alpha"),
                                   boot_cwd_raw="@HOME@/custom/\n", claude_json=cj)
    assert argv[-1] == str(home / "custom")
    assert _boot_lines(proc) == [f"boot cwd: {home}/custom (from boot-cwd)"]
    assert _errors(proc) == [], proc.stderr


# ---- round 3: trust-aware selection S1-S6 ----------------------------------

def _trust(*names, value=True):
    return {"projects": {f"@HOME@/{n}" if n != "." else "@HOME@": {"hasTrustDialogAccepted": value}
                         for n in names}}


def test_S1_only_umwelt_trusted_picks_umwelt_no_error(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "umwelt"),
                                   claude_json=_trust("umwelt"))
    assert proc.returncode == 0, proc.stderr
    assert argv[-1] == str(home / "umwelt")
    assert _boot_lines(proc) == [f"boot cwd: {home}/umwelt (from $HOME/umwelt, first trusted)"]
    assert _errors(proc) == [], proc.stderr


def test_S7_home_with_ampersand_still_finds_trusted_umwelt(tmp_path):
    """S7: a $HOME containing '&' must not corrupt candidate paths (bash patsub_replacement)."""
    base = tmp_path / "a&b"
    base.mkdir()
    proc, home, argv, *_ = _launch(base, dirs=("engram-alpha", "umwelt"),
                                   claude_json=_trust("umwelt"))
    assert "&" in str(home)
    assert argv[-1] == str(home / "umwelt")
    assert _errors(proc) == [], proc.stderr


def test_S2_both_trusted_order_breaks_tie(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "umwelt"),
                                   claude_json=_trust("engram-alpha", "umwelt"))
    assert argv[-1] == str(home / "engram-alpha")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram-alpha (from $HOME/engram-alpha, first trusted)"]
    assert _errors(proc) == [], proc.stderr


def test_S3_neither_trusted_engram_alpha_plus_error(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "umwelt"),
                                   claude_json={"projects": {}})
    assert argv[-1] == str(home / "engram-alpha")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram-alpha (from $HOME/engram-alpha, none trusted)"]
    _assert_untrusted(proc, home / "engram-alpha")


def test_S4_only_home_trusted_picks_home_no_error(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha",),
                                   claude_json=_trust("."))
    assert argv[-1] == str(home)
    assert _boot_lines(proc) == [f"boot cwd: {home} (from $HOME, first trusted)"]
    assert _errors(proc) == [], proc.stderr


def test_S5_explicit_untrusted_boot_cwd_beats_trusted_umwelt(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("custom", "umwelt"),
                                   boot_cwd="@HOME@/custom", claude_json=_trust("umwelt"))
    assert proc.returncode == 0, proc.stderr
    assert argv[-1] == str(home / "custom")
    assert _boot_lines(proc) == [f"boot cwd: {home}/custom (from boot-cwd)"]
    _assert_untrusted(proc, home / "custom")


def test_S6_claude_config_dir_falls_through_to_order_plus_error(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "umwelt", "cfg"),
                                   claude_json=_trust("umwelt"),
                                   extra_env={"CLAUDE_CONFIG_DIR": "@HOME@/cfg"})
    assert argv[-1] == str(home / "engram-alpha")
    errs = _errors(proc)
    assert len(errs) == 1 and "CLAUDE_CONFIG_DIR" in errs[0], proc.stderr
