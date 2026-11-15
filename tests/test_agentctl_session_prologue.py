"""Tests for agentctl's shared session prologue (#3451).

`agentctl __session-prologue` prints the shell text every `cmd_session` launch
branch runs in the TARGET user's shell before `cd`/`exec`. The tests eval that
exact text under a fake HOME (no sudo, no real agents) and observe a child
process, so they discriminate what the launched process would see.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# One shared probe for the GNU `stat -c` primitive (defined with the hint tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agentctl_forum_hint_3563 import needs_gnu_stat  # noqa: E402

AGENTCTL = Path(__file__).resolve().parent.parent / "tools" / "agentctl"


def _prologue_text() -> str:
    proc = subprocess.run(
        ["bash", str(AGENTCTL), "__session-prologue"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "prologue text is empty"
    return proc.stdout


def _launch(home: Path, child: str, start_umask: str = "0002"):
    """Run `umask <start>; <prologue>; <child>` in bash with HOME=home."""
    script = f"umask {start_umask}\n{_prologue_text()}\n{child}\n"
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _boot_env(home: Path, content: str, mode: int = 0o644) -> Path:
    d = home / ".engram"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "boot-env"
    f.write_text(content)
    f.chmod(mode)
    return f


def test_boot_env_absent_runs_clean_and_umask_022(tmp_path):
    # Absence is the normal state for most seats: a true optional, not a
    # hidden failure -- no message, exit 0.
    proc = _launch(tmp_path, "sh -c umask")
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout.strip() == "0022"


def test_boot_env_present_exports_vars_to_child(tmp_path):
    _boot_env(tmp_path, "FOO_FROM_BOOT_ENV=bar\nQUOTED_FROM_BOOT_ENV='a b  c'\n")
    proc = _launch(tmp_path, "env")
    assert proc.returncode == 0
    assert proc.stderr == ""
    lines = proc.stdout.splitlines()
    assert "FOO_FROM_BOOT_ENV=bar" in lines
    assert "QUOTED_FROM_BOOT_ENV=a b  c" in lines


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_boot_env_unreadable_warns_once_and_launch_proceeds(tmp_path):
    f = _boot_env(tmp_path, "FOO_FROM_BOOT_ENV=bar\n", mode=0o000)
    try:
        proc = _launch(tmp_path, "env")
    finally:
        f.chmod(0o644)
    assert proc.returncode == 0
    err = [l for l in proc.stderr.splitlines() if l]
    assert len(err) == 1
    assert "boot-env" in err[0] and "not readable" in err[0]
    assert "FOO_FROM_BOOT_ENV" not in proc.stdout


# --- Host-common boot-env (sourced BEFORE the per-agent boot-env) ----------

def _launch_common(home: Path, child: str, common: Path):
    """Like _launch, with AGENT_BOOT_ENV_COMMON pointing at `common`."""
    script = f"umask 0002\n{_prologue_text()}\n{child}\n"
    env = {"PATH": os.environ["PATH"], "HOME": str(home),
           "AGENT_BOOT_ENV_COMMON": str(common)}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _common(tmp_path: Path, content: str, mode: int = 0o644) -> Path:
    f = tmp_path / "shared" / "boot-env.common"
    f.parent.mkdir(exist_ok=True)
    f.write_text(content)
    f.chmod(mode)
    return f


def test_common_per_agent_value_wins(tmp_path):
    home = tmp_path / "home"
    _boot_env(home, "SHARED_VAR=agent\n")
    common = _common(tmp_path, "SHARED_VAR=common\nCOMMON_ONLY=yes\n")
    proc = _launch_common(home, "env", common)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "SHARED_VAR=agent" in lines
    assert "SHARED_VAR=common" not in lines
    # Common-only value is exported to the child.
    assert "COMMON_ONLY=yes" in lines


def test_common_absent_is_silent_and_unchanged(tmp_path):
    home = tmp_path / "home"
    _boot_env(home, "FOO_FROM_BOOT_ENV=bar\n")
    proc = _launch_common(home, "env", tmp_path / "does-not-exist")
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "FOO_FROM_BOOT_ENV=bar" in proc.stdout.splitlines()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_common_unreadable_warns_once_and_per_agent_still_sourced(tmp_path):
    home = tmp_path / "home"
    _boot_env(home, "FOO_FROM_BOOT_ENV=bar\n")
    common = _common(tmp_path, "COMMON_ONLY=yes\n", mode=0o000)
    try:
        proc = _launch_common(home, "env", common)
    finally:
        common.chmod(0o644)
    assert proc.returncode == 0
    err = [l for l in proc.stderr.splitlines() if l]
    assert len(err) == 1
    assert "WARNING" in err[0] and str(common) in err[0] and "not readable" in err[0]
    lines = proc.stdout.splitlines()
    assert "FOO_FROM_BOOT_ENV=bar" in lines
    assert "COMMON_ONLY=yes" not in lines


def test_common_hostile_content_does_not_abort_launch(tmp_path):
    # Mirrors the per-agent policy: a failing command / unset reference in the
    # sourced file must not stop the launch (the prologue runs without set -e).
    home = tmp_path / "home"
    _boot_env(home, "SURVIVOR_FROM_BOOT_ENV=yes\n")
    common = _common(tmp_path, "B=$NOPE_UNSET\nfalse\n")
    proc = _launch_common(home, "env", common)
    assert proc.returncode == 0, proc.stderr
    assert "SURVIVOR_FROM_BOOT_ENV=yes" in proc.stdout.splitlines()


@needs_gnu_stat
def test_umask_forced_to_022_from_0002(tmp_path):
    proc = _launch(tmp_path, "sh -c umask", start_umask="0002")
    assert proc.stdout.strip() == "0022"
    # And a file the child creates is 644, not 664.
    proc = _launch(tmp_path, f"touch {tmp_path}/f; stat -c %a {tmp_path}/f", start_umask="0002")
    assert proc.stdout.strip() == "644"


def test_as_self_form_is_insulated_from_boot_env_errors(tmp_path):
    # The as-self branch launches via `bash -c "$prologue; ..."` (same form as
    # the sudo branches). A boot-env with an unset-variable reference and a
    # failing command must not abort the launch, even when the PARENT shell
    # runs under `set -euo pipefail` like agentctl itself.
    _boot_env(tmp_path, "B=$NOPE_UNSET\nfalse\nSURVIVOR_FROM_BOOT_ENV=yes\n")
    script = (
        "set -euo pipefail\n"
        'prologue="$(bash "$AGENTCTL" __session-prologue)"\n'
        'bash -c "$prologue; exec env" --\n'
    )
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "AGENTCTL": str(AGENTCTL)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "SURVIVOR_FROM_BOOT_ENV=yes" in proc.stdout.splitlines()


def _cmd_session_body() -> str:
    text = AGENTCTL.read_text()
    m = re.search(r"^cmd_session\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "cmd_session body not found"
    return m.group(0)


def test_every_claude_launch_site_carries_the_prologue():
    """Wiring: each `exec` launch site for Claude in cmd_session runs `$prologue;`.

    Eight sites: as-self binary +/- cwd (2), as-self start.sh +/- cwd (2),
    sudo binary +/- cwd (2), sudo start.sh +/- cwd (2). The Codex managed
    branch (`__codex-managed-session`) must NOT carry the prologue.
    """
    body = _cmd_session_body()
    assert body.count("$prologue;") == 8
    codex_start = body.index("__codex-managed-session")
    claude_start = body.index('prologue="$(session_prologue_text)"')
    assert codex_start < claude_start
    assert "$prologue" not in body[:claude_start]
    # A prologue reaching the Codex branch by direct call is caught too.
    assert "session_prologue_text" not in body[:claude_start]
    assert "__session-prologue" not in body[:claude_start]


# Each Claude launch site is one line of the cmd_session body carrying one of
# these exec forms (the quoting differs per site, so match the unquoted tail).
# The two direct forms are listed on purpose: a site that execs the binary
# directly (no scope verb, #4435) is still FOUND as a launch site, then fails
# `_check_scope_exec_word` instead of silently dropping out of the count.
_CLAUDE_EXEC_MARKERS = (
    'exec \\"$binary\\"', "exec ~/.runtime/start.sh", "exec $scope_words",
)


def _check_scope_exec_word(line: str) -> None:
    """Assert the FIRST `exec` on the line execs `$scope_words` (the abs-agentctl
    + `__scope-exec` + seat words the outer shell inserted), directly followed
    by the launch target (`"$binary"` or the start.sh adapter)."""
    where = f"launch site: {line.strip()!r}"
    m = re.search(r"exec (\S+)", line)
    assert m, f"no exec on the line -- {where}"
    assert m.group(1) == "$scope_words", (
        f"exec does not go through `$scope_words` (__scope-exec) -- {where}"
    )
    after = line[m.end():]
    assert re.match(r"""\s*(\\"\$binary\\"|"'~/\.runtime/start\.sh|~/\.runtime/start\.sh)""", after), (
        f"`$scope_words` is not directly followed by the launch target -- {where}"
    )


def _check_launch_site_order(line: str) -> None:
    """Assert `$prologue;` precedes `cd` and `exec` within ONE launch-site line.

    Failure messages quote the offending line, so the broken site is named.
    """
    where = f"launch site: {line.strip()!r}"
    p = line.find("$prologue;")
    assert p != -1, f"no `$prologue;` on the same line as the exec -- {where}"
    exec_at = min(i for i in (line.find(m) for m in _CLAUDE_EXEC_MARKERS) if i != -1)
    assert p < exec_at, f"`$prologue;` is not before `exec` -- {where}"
    _check_scope_exec_word(line)
    # Any `cd` command, whatever its argument form: a `cd` token not glued to a
    # word, `$`, `.`, `/` or `-` on its left (so not `abcd`, `$cd`, `./cd`) and
    # followed by whitespace.
    cd_m = re.search(r"(?<![\w$./-])cd\s", line)
    cd_at = cd_m.start() if cd_m else -1
    if cd_at != -1:
        assert p < cd_at, f"`$prologue;` is not before `cd` -- {where}"
        assert cd_at < exec_at, f"`cd` is not before `exec` -- {where}"


def test_every_claude_launch_site_runs_the_prologue_before_cd_and_exec():
    """Order, per site: the prologue comes before `cd` and `exec` on that line.

    The count cell above passes if `$prologue;` moves AFTER the `exec` (still 8).
    The end-to-end cells drive only the `--as-self` sites; no cell can run the
    four sudo sites (no sudo in the sandboxes), so this static, per-line check
    is the only protection for those. Also: exactly 8 lines carry a Claude
    `exec`, and each of them carries `$prologue;` itself (a stray `$prologue;`
    on some other line, such as a comment, cannot stand in for a dropped one).
    """
    body = _cmd_session_body()
    launch_lines = [
        l for l in body.splitlines() if any(m in l for m in _CLAUDE_EXEC_MARKERS)
    ]
    assert len(launch_lines) == 8, launch_lines
    for line in launch_lines:
        _check_launch_site_order(line)


# Synthetic launch-site lines for `_check_launch_site_order` (no file reads).
# The four real shapes below are each used twice in cmd_session (as-self and
# sudo carry identical text), so four distinct strings cover the eight sites.
_ORDER_PASS_LINES = [
    '"$prologue; cd $(printf \'%q\' "${cwd_arg[0]}") && exec $scope_words \\"$binary\\" \\"\\$@\\""',
    '"$prologue; exec $scope_words \\"$binary\\" \\"\\$@\\""',
    '"$prologue; cd $(printf \'%q\' "${cwd_arg[0]}") && exec $scope_words ~/.runtime/start.sh \\"\\$@\\""',
    '"$prologue; exec $scope_words "\'~/.runtime/start.sh "$@"\'',
    # Any cd form is fine as long as the prologue and exec order holds.
    '"$prologue; cd . && exec $scope_words \\"$binary\\" \\"\\$@\\""',
    # `cd` inside another word, as a variable, or as a path component is not a
    # cd command (guards the regex's left boundary).
    '"$prologue; exec $scope_words \\"$binary\\" \\"\\$@\\"; echo abcd $cd ./cd x"',
]
_ORDER_RAISE_LINES = [
    # cd before the prologue (a cd form other than `cd $(printf`).
    '"cd . && $prologue; exec $scope_words \\"$binary\\" \\"\\$@\\""',
    '"cd \\"$d\\" && $prologue; exec $scope_words ~/.runtime/start.sh \\"\\$@\\""',
    # prologue after exec.
    '"exec $scope_words \\"$binary\\" \\"\\$@\\"; $prologue;"',
    # exec with no prologue at all.
    '"exec $scope_words \\"$binary\\" \\"\\$@\\""',
    # cd after exec (prologue first).
    '"$prologue; exec $scope_words \\"$binary\\" \\"\\$@\\"; cd . "',
]
# #4435: a launch site that execs the target directly skips the OOMPolicy scope.
# Prologue and cd order are correct on every one of these; only the scope word
# is missing or misplaced, so these discriminate `_check_scope_exec_word`.
_SCOPE_RAISE_LINES = [
    '"$prologue; exec \\"$binary\\" \\"\\$@\\""',
    '"$prologue; cd $(printf \'%q\' "${cwd_arg[0]}") && exec \\"$binary\\" \\"\\$@\\""',
    '"$prologue; cd $(printf \'%q\' "${cwd_arg[0]}") && exec ~/.runtime/start.sh \\"\\$@\\""',
    '"$prologue; "\'exec ~/.runtime/start.sh "$@"\'',
    # the scope words are present but are not what is exec'd
    '"$prologue; echo $scope_words; exec \\"$binary\\" \\"\\$@\\""',
    # scope words not followed by a launch target
    '"$prologue; exec $scope_words true"',
]


@pytest.mark.parametrize("line", _ORDER_PASS_LINES)
def test_launch_site_order_check_accepts_correct_lines(line):
    _check_launch_site_order(line)


@pytest.mark.parametrize("line", _ORDER_RAISE_LINES)
def test_launch_site_order_check_rejects_misordered_lines(line):
    with pytest.raises(AssertionError):
        _check_launch_site_order(line)


@pytest.mark.parametrize("line", _SCOPE_RAISE_LINES)
def test_launch_site_order_check_rejects_sites_that_skip_the_scope_verb(line):
    with pytest.raises(AssertionError, match="scope_words|launch target"):
        _check_launch_site_order(line)


def test_scope_words_are_built_from_abs_agentctl_and_the_quoted_seat():
    """The words the outer shell inserts name the verb and the seat, each
    quoted with `_oct_quote` (#4416; never raw, never a `$`-expansion in the inserted
    text itself)."""
    body = _cmd_session_body()
    want = ('scope_words="$(_oct_quote "$agentctl_abs") __scope-exec '
            '$(_oct_quote "$name")"')
    assert want in body, "scope_words is not abs-agentctl + __scope-exec + octal-quoted seat"
    assert re.search(r"agentctl_abs=.*readlink -f", body)
    assert "cannot resolve" in body  # fails loudly when the path does not resolve


# --- End-to-end: the REAL cmd_session under --as-self, no sudo, no real agent.
# A fake `id` on PATH satisfies resolve_user and _as_self_check on any host;
# a fake `claude` (or ~/.runtime/start.sh) prints argv, pwd and the SURV env.

_FAKE_ID = '#!/bin/sh\nif [ "$1" = "-un" ]; then echo agent-zed; else exit 0; fi\n'
# #4435: records its argv and runs the part after `--`; the probe says `inactive`.
_FAKE_SYSTEMD_RUN = (
    '#!/bin/bash\nprintf "%s\\0" "$@" >> "$FAKE_REC/sdrun.argv"\n'
    'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n'
)
_FAKE_SYSTEMCTL = '#!/bin/sh\necho inactive\n'
_FAKE_LAUNCHEE = '#!/bin/sh\necho "ARGV:$*"; echo "PWD:$(pwd)"; env | grep ^SURV\n'


def _exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def _run_as_self(tmp_path: Path, *, claude_on_path: bool, cd: Path | None):
    """Run `agentctl session zed --platform claude --as-self [--cd d] -c`."""
    home = tmp_path / "home"
    home.mkdir()
    _boot_env(home, "B=$NOPE_UNSET\nfalse\nSURV=yes\n")
    bindir = tmp_path / "bin"
    _exe(bindir / "id", _FAKE_ID)
    # #4435: the launch goes through `__scope-exec`, which would create a REAL
    # `agent-claude-zed` scope on a systemd host. Fakes first on PATH.
    rec = tmp_path / "rec"
    rec.mkdir()
    _exe(bindir / "systemd-run", _FAKE_SYSTEMD_RUN)
    _exe(bindir / "systemctl", _FAKE_SYSTEMCTL)
    if claude_on_path:
        _exe(bindir / "claude", _FAKE_LAUNCHEE)
    else:
        _exe(home / ".runtime" / "start.sh", _FAKE_LAUNCHEE)
    args = ["bash", str(AGENTCTL), "session", "zed", "--platform", "claude", "--as-self"]
    if cd is not None:
        args += ["--cd", str(cd)]
    args += ["-c"]
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home),
           "FAKE_REC": str(rec), "XDG_RUNTIME_DIR": str(tmp_path)}
    return subprocess.run(args, capture_output=True, text=True, env=env)


def test_as_self_e2e_with_cd_survives_hostile_boot_env(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    proc = _run_as_self(tmp_path, claude_on_path=True, cd=proj)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "ARGV:--continue" in lines
    assert f"PWD:{proj.resolve()}" in lines
    assert "SURV=yes" in lines


def test_as_self_e2e_without_cd(tmp_path):
    proc = _run_as_self(tmp_path, claude_on_path=True, cd=None)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "ARGV:--continue" in lines
    assert any(l.startswith("PWD:") for l in lines)
    assert "SURV=yes" in lines


def test_as_self_e2e_goes_through_the_scope_unit(tmp_path):
    """#4435: the as-self launch reaches the child via systemd-run with the
    seat's own unit name (both the binary and the start.sh adapter branch)."""
    for claude_on_path in (True, False):
        sub = tmp_path / ("bin_branch" if claude_on_path else "adapter_branch")
        sub.mkdir()
        proc = _run_as_self(sub, claude_on_path=claude_on_path, cd=None)
        assert proc.returncode == 0, proc.stderr
        argv = (sub / "rec" / "sdrun.argv").read_bytes().split(b"\0")[:-1]
        assert b"--unit=agent-claude-zed" in argv
        assert b"OOMPolicy=continue" in argv
        assert "ARGV:--continue" in proc.stdout.splitlines()


def test_as_self_e2e_start_sh_adapter_branch(tmp_path):
    proc = _run_as_self(tmp_path, claude_on_path=False, cd=None)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "ARGV:--continue" in lines
    assert "SURV=yes" in lines
