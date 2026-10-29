"""#4255 slice 4: the tools stop speaking the retired multi-agent axis.

Contract S4 (design owner Ariadne) plus the vocabulary rule: in shipped files the old words go
entirely, except the retirement-message line itself.

  A  ``agent-bootstrap --multi-agent`` is accepted and ignored. It prints exactly one stderr line,
     ``--multi-agent is retired and has no effect (#4255)``, never on stdout, once however often
     the flag is repeated; the exit status is the status without the flag; install.sh (stubbed,
     it records its argv) never receives ``--multi-agent`` while ``--tier`` / ``--forum-url`` still
     arrive. Varied: LC_ALL=C, a TMPDIR reached through a symlink, a cwd that is not the repo,
     umask 077, and a failing install.sh (same exit status with and without the flag).
  B  ``agentctl spawn`` hands agent-bootstrap ``--tier dev`` and no ``--multi-agent``. This is
     BEHAVIOUR: the real spawn verb runs in a throwaway user+mount namespace with a stubbed
     agent-bootstrap that records its argv (the harness of test_agentctl_forum_hint_3563). It
     skips where unprivileged user namespaces are unavailable; the text cell B2 pins the source
     line regardless and is TEXT, not behaviour.
  C  (lives in tests/test_baton.py::TestFlipColleagueApprovalGate: importing test_baton from
     here would leak its sys.path edits into this module) the baton flip warning for a baton
     with no colleague carries the new wording.
  D  none of the touched files contains the old words outside the retirement line.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_bootstrap_install_4097 import make_env, make_source  # noqa: E402
from test_agentctl_forum_hint_3563 import _read, _spawn, needs_userns  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "tools" / "agent-bootstrap"
AGENTCTL = REPO / "tools" / "agentctl"

RETIRED = "--multi-agent is retired and has no effect (#4255)"


# ---------------------------------------------------------------------------------------------
# A: agent-bootstrap (sourced in lib-only mode, the 4097 harness; fake sudo, stub install.sh)
# ---------------------------------------------------------------------------------------------
def _bootstrap(tmp_path: Path, flags: str, *, variant: str = "plain", install_rc: int = 0):
    """Parse ``flags`` with the real ab_parse_args, then run the real install phase.

    Returns (proc, rc, argv_line) where argv_line is what the stub install.sh recorded.
    """
    env, home = make_env(tmp_path)
    cwd = None
    umask = None
    if variant == "lc_all_c":
        env["LC_ALL"] = "C"
    elif variant == "tmpdir_symlink":
        link = tmp_path / "tmp-link"
        link.symlink_to(Path(env["TMPDIR"]))
        env["TMPDIR"] = str(link)
    elif variant == "cwd_elsewhere":
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
    elif variant == "umask_077":
        umask = 0o077
    else:
        assert variant == "plain", variant
    if install_rc:
        env["FAKE_INSTALL_RC"] = str(install_rc)
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    body = (f'ab_parse_args nb --runtime claude {flags}\n'
            f'USER_NAME="agent-nb"; USER_HOME="{home}"; ALPHA_SOURCE="{src}"\n'
            'rc=0; ab_clone_and_install || rc=$?; echo "RC=$rc"')
    script = f'set -u\nsource "{BOOTSTRAP}"\n{body}\n'

    def _pre():
        if umask is not None:
            os.umask(umask)

    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                          timeout=120, cwd=cwd, preexec_fn=_pre)
    m = re.search(r"^RC=(\d+)$", proc.stdout, re.M)
    assert m, proc.stdout + proc.stderr
    log = tmp_path / "install.log"
    argv = ""
    if log.exists():
        argv = next((ln for ln in log.read_text().splitlines() if ln.startswith("argv:")), "")
    return proc, int(m.group(1)), argv


def _sub(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


VARIANTS = ["plain", "lc_all_c", "tmpdir_symlink", "cwd_elsewhere", "umask_077"]


class TestAAgentBootstrapRetiredFlag:
    @pytest.mark.parametrize("variant", VARIANTS)
    def test_line_once_on_stderr_only_and_nothing_forwarded(self, tmp_path, variant):
        proc, rc, argv = _bootstrap(_sub(tmp_path, "flag"), "--tier dev --multi-agent",
                                    variant=variant)
        assert rc == 0, proc.stdout + proc.stderr
        assert proc.stderr.count(RETIRED) == 1, proc.stderr
        assert "retired" not in proc.stdout and "multi-agent" not in proc.stdout, proc.stdout
        assert argv, "install.sh stub never recorded an argv"
        assert "multi-agent" not in argv, argv
        assert "--tier dev" in argv, argv

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_result_is_the_result_without_the_flag(self, tmp_path, variant):
        with_p, with_rc, with_argv = _bootstrap(_sub(tmp_path, "with"), "--tier dev --multi-agent",
                                                variant=variant)
        no_p, no_rc, no_argv = _bootstrap(_sub(tmp_path, "without"), "--tier dev", variant=variant)
        assert with_rc == no_rc == 0
        assert with_argv == no_argv
        assert RETIRED not in no_p.stderr
        # nothing else differs on stderr: drop the one line and compare, tmp paths normalised
        norm = lambda p, d: p.stderr.replace(RETIRED + "\n", "").replace(str(d), "TMP")  # noqa: E731
        assert norm(with_p, tmp_path / "with") == norm(no_p, tmp_path / "without")

    def test_repeated_flag_prints_one_line(self, tmp_path):
        proc, rc, argv = _bootstrap(tmp_path, "--multi-agent --tier dev --multi-agent")
        assert rc == 0, proc.stdout + proc.stderr
        assert proc.stderr.count(RETIRED) == 1, proc.stderr
        assert "multi-agent" not in argv

    def test_forum_url_still_reaches_install_sh(self, tmp_path):
        proc, rc, argv = _bootstrap(
            tmp_path, "--multi-agent --tier dev --forum-url http://f.example:5002")
        assert rc == 0, proc.stdout + proc.stderr
        assert proc.stderr.count(RETIRED) == 1
        assert "--forum-url http://f.example:5002" in argv, argv
        assert "--tier dev" in argv and "multi-agent" not in argv, argv

    @pytest.mark.parametrize("variant", ["plain", "lc_all_c", "umask_077"])
    def test_failing_install_exits_the_same_with_and_without_the_flag(self, tmp_path, variant):
        with_p, with_rc, _ = _bootstrap(_sub(tmp_path, "with"), "--multi-agent",
                                        variant=variant, install_rc=7)
        no_p, no_rc, _ = _bootstrap(_sub(tmp_path, "without"), "", variant=variant, install_rc=7)
        assert with_rc == no_rc == 7, (with_p.stderr, no_p.stderr)
        assert with_p.stderr.count(RETIRED) == 1

    def test_no_flag_prints_no_retirement_line(self, tmp_path):
        proc, rc, _ = _bootstrap(tmp_path, "--tier dev")
        assert rc == 0
        assert "retired" not in proc.stderr

    def test_flag_is_not_an_unknown_flag(self, tmp_path):
        # The flag never exits 2 and is never reported as unknown.
        proc, rc, _ = _bootstrap(tmp_path, "--multi-agent")
        assert rc == 0
        assert "Unknown flag" not in proc.stderr

    def test_the_script_itself_prints_the_line_before_the_root_check(self, tmp_path):
        # Non-lib run: the root check comes after arg parsing, so a non-root run shows both the
        # retirement line and the root refusal, and the refusal's status (2) is the status
        # without the flag. Skipped as root: there the script would go on to create a user.
        if os.geteuid() == 0:
            pytest.skip("running as root: the real script would create a user")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
        base = ["bash", str(BOOTSTRAP), "nb", "--runtime", "claude", "--no-install"]
        a = subprocess.run(base, capture_output=True, text=True, env=env, timeout=60)
        b = subprocess.run(base + ["--multi-agent"], capture_output=True, text=True, env=env,
                           timeout=60)
        assert a.returncode == b.returncode == 2
        assert b.stderr.count(RETIRED) == 1 and RETIRED not in a.stderr
        assert b.stdout == a.stdout

    def test_help_does_not_list_the_flag(self):
        r = subprocess.run(["bash", str(BOOTSTRAP), "--help"], capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0
        assert "multi-agent" not in r.stdout and "multi-agent" not in r.stderr


# ---------------------------------------------------------------------------------------------
# B: agentctl spawn
# ---------------------------------------------------------------------------------------------
class TestBAgentctlSpawn:
    @needs_userns
    def test_spawn_hands_bootstrap_tier_dev_and_no_multi_agent(self, tmp_path):
        proc, state, _ = _spawn(tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        called = _read(state, "bootstrap-called")
        assert called is not None, "the bootstrap stub was never called"
        assert " --tier dev" in called, called
        assert "multi-agent" not in called, called
        assert "retired" not in proc.stderr and "retired" not in proc.stdout

    @needs_userns
    def test_forum_url_still_passed_and_still_no_flag(self, tmp_path):
        proc, state, _ = _spawn(tmp_path, "--forum-url", "http://forum.example:5002")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        called = _read(state, "bootstrap-called")
        assert "--forum-url http://forum.example:5002" in called, called
        assert " --tier dev" in called and "multi-agent" not in called, called

    def test_b2_TEXT_ONLY_the_bootstrap_args_line(self):
        # TEXT, not behaviour: pins the one source line, for hosts where B cannot run.
        lines = [ln for ln in AGENTCTL.read_text().splitlines()
                 if 'bootstrap_args+=("--tier"' in ln]
        assert lines == ['    bootstrap_args+=("--tier" "dev")'], lines


# ---------------------------------------------------------------------------------------------
# D: the words are gone from every touched file (the retirement line is the one exemption)
# ---------------------------------------------------------------------------------------------
TOUCHED = [
    "src/engram/tools/baton.py",
    "src/engram/tools/write_sleep_marker.py",
    "src/engram/tools/cohort_dispatch.py",
    "tools/agent-bootstrap",
    "tools/agentctl",
    "tools/agentctl-mac",
    "src/engram/tools/pre-dispatch-check.sh",
    "src/engram/tools/operator-setup-viz.sh",
]
OLD_WORDS = re.compile(r"multi[-_ ]?agent|single[-_ ]?agent", re.I)


def _offenders(text: str) -> list[str]:
    bad = []
    for n, ln in enumerate(text.splitlines(), 1):
        if RETIRED in ln:
            # the retirement line (here also the case label that triggers it) is allowed
            ln = ln.replace(RETIRED, "").replace("--multi-agent)", "")
        if OLD_WORDS.search(ln):
            bad.append(f"{n}: {ln.strip()}")
    return bad


class TestDVocabulary:
    @pytest.mark.parametrize("rel", TOUCHED)
    def test_no_old_words(self, rel):
        assert _offenders((REPO / rel).read_text()) == []

    def test_the_checker_discriminates(self):
        assert _offenders("# a multi-agent host\n") != []
        assert _offenders("# Single-agent mode\n") != []
        assert _offenders("x = multi_agent\n") != []
        assert _offenders(f'--multi-agent) echo "{RETIRED}" >&2 ;;\n') == []
        assert _offenders(f'echo "{RETIRED}"; # but also multi-agent\n') != []
        assert _offenders('--multi-agent) shift ;;\n') != []

    def test_agent_bootstrap_has_exactly_one_retirement_line(self):
        assert (BOOTSTRAP.read_text()).count(RETIRED) == 1
