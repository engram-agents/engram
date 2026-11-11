"""agent-bootstrap --sandbox-build-ref (#4097, slice 5 round 3, S1): for the #4101 sandbox rig.

Contract (written before the code):
  * ``--sandbox-build-ref REF`` skips the deployable-tag pick and checks out exactly REF
    (refs/remotes/origin/REF, then a tag, then a full/short SHA) detached, in the clone.
  * An unresolvable REF FAILs naming the ref; install.sh is not run.
  * Every sandbox run prints ``SANDBOX BUILD (not a release): <ref> = <full sha>`` on stdout
    before install.sh runs, and as the FIRST line of the next-steps block.
  * install.sh gets ``--sandbox-untagged`` iff the flag was given; never --allow-untagged /
    --allow-branch directly.
  * Only the flag turns sandbox mode on: no environment variable does.
  * Without the flag: tag-only, unchanged (the existing cells).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_bootstrap_install_4097 import (  # noqa: E402
    BOOTSTRAP, SRC, CODE, _git, _rc, install_body, make_env, make_source, run_lib,
)

BANNER = "SANDBOX BUILD (not a release)"


def _install_log(tmp_path):
    return (tmp_path / "install.log").read_text()


def test_branch_ref_checks_out_exactly_that_commit_without_any_tag(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path)  # no tags at all; dev head untagged
    sha = _git(src, "rev-parse", "dev")
    proc = run_lib(env, install_body(home, src, 'SANDBOX_REF="dev"'))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    assert _git(home / "engram-alpha", "rev-parse", "HEAD") == sha
    assert f"{BANNER}: dev = {sha}" in proc.stdout


def test_banner_printed_before_install_runs_and_install_gets_sandbox_untagged(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path)
    proc = run_lib(env, install_body(home, src, 'SANDBOX_REF="dev"'))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    argv = [ln for ln in _install_log(tmp_path).splitlines() if ln.startswith("argv:")][0]
    assert "--sandbox-untagged" in argv
    assert "--allow-untagged" not in argv and "--allow-branch" not in argv
    assert proc.stdout.index(BANNER) < proc.stdout.index("Running tools/install.sh")


def test_short_sha_ref_resolves_to_the_full_sha(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path)
    full = _git(src, "rev-parse", "dev~1")
    proc = run_lib(env, install_body(home, src, f'SANDBOX_REF="{full[:9]}"'))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    assert _git(home / "engram-alpha", "rev-parse", "HEAD") == full
    assert f"{BANNER}: {full[:9]} = {full}" in proc.stdout


def test_tag_ref_resolves(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path, tags={"v0.1.0": "c1"})
    proc = run_lib(env, install_body(home, src, 'SANDBOX_REF="v0.1.0"'))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    assert _git(home / "engram-alpha", "rev-parse", "HEAD") == _git(src, "rev-parse", "v0.1.0^{commit}")


def test_sandbox_ref_skips_the_tag_pick_even_when_a_deployable_tag_exists(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    sha = _git(src, "rev-parse", "dev")
    proc = run_lib(env, install_body(home, src, 'SANDBOX_REF="dev"'))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    assert _git(home / "engram-alpha", "rev-parse", "HEAD") == sha  # head, not the tag at c2


def test_unknown_ref_fails_naming_the_ref_and_runs_nothing(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    proc = run_lib(env, install_body(home, src, 'SANDBOX_REF="no-such-ref"'))
    assert _rc(proc) != 0
    assert "no-such-ref" in proc.stderr
    assert not (tmp_path / "install.log").exists()
    assert "left in place" in proc.stderr
    assert BANNER not in proc.stdout


def test_ref_that_looks_like_an_option_is_refused(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path)
    proc = run_lib(env, install_body(home, src, 'SANDBOX_REF="--upload-pack=x"'))
    assert _rc(proc) != 0 and not (tmp_path / "install.log").exists()


def test_without_the_flag_install_gets_no_sandbox_flag_and_no_banner(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    proc = run_lib(env, install_body(home, src))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    assert "--sandbox-untagged" not in _install_log(tmp_path)
    assert BANNER not in proc.stdout


def test_env_vars_cannot_turn_sandbox_mode_on(tmp_path):
    env, home = make_env(tmp_path)
    env.update({"SANDBOX_BUILD_REF": "dev", "ENGRAM_SANDBOX_BUILD_REF": "dev", "SANDBOX_REF": "dev"})
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    body = install_body(home, src).replace('TIER=""', 'unset SANDBOX_REF; TIER=""')
    proc = run_lib(env, body)
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    assert "--sandbox-untagged" not in _install_log(tmp_path) and BANNER not in proc.stdout
    assert 'SANDBOX_REF="${SANDBOX_REF' not in SRC  # never seeded from the environment
    assert not re.search(r"SANDBOX_REF=.*ENGRAM_|SANDBOX_REF=\$\{[A-Z_]+:-", CODE)


def test_lacking_install_sh_at_the_ref_fails_before_running_anything(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path, installer_at="c3")
    proc = run_lib(env, install_body(home, src, f'SANDBOX_REF="{_git(src, "rev-parse", "dev~1")}"'))
    assert _rc(proc) != 0
    assert not (tmp_path / "install.log").exists()


def _next_steps(tmp_path, extra):
    env, _home = make_env(tmp_path)
    proc = run_lib(env, ('NAME=nb; USER_NAME=agent-alice; USER_HOME=/home/agent-alice; RUNTIME=claude\n'
                         'HOME_MODE=0750; ALPHA_SOURCE=/x/alpha; NO_INSTALL=0\n'
                         f'{extra}\nab_print_next_steps'))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_banner_is_the_first_line_of_the_next_steps_block(tmp_path):
    sha = "a" * 40
    out = _next_steps(tmp_path, f'SANDBOX_REF=dev; SANDBOX_BANNER="{BANNER}: dev = {sha}"')
    assert out.splitlines()[0] == f"{BANNER}: dev = {sha}"


def test_no_banner_in_next_steps_without_sandbox(tmp_path):
    assert BANNER not in _next_steps(tmp_path, 'SANDBOX_REF=""; SANDBOX_BANNER=""')


def test_full_run_prints_the_banner_in_both_places(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path)
    sha = _git(src, "rev-parse", "dev")
    body = install_body(home, src, 'SANDBOX_REF="dev"').replace(
        'rc=0; ab_clone_and_install', 'NAME=nb; RUNTIME=claude; HOME_MODE=0750; NO_INSTALL=0\n'
        'rc=0; ab_clone_and_install').replace('echo "RC=$rc"', 'echo "RC=$rc"; ab_print_next_steps')
    proc = run_lib(env, body)
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    assert proc.stdout.count(f"{BANNER}: dev = {sha}") == 2, proc.stdout


def test_parse_accepts_the_flag_and_sets_the_ref():
    import subprocess
    script = f'source "{BOOTSTRAP}"\nab_parse_args nb --runtime claude --sandbox-build-ref dev\necho "REF=$SANDBOX_REF"'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "AGENT_BOOTSTRAP_LIB_ONLY": "1"})
    assert "REF=dev" in r.stdout, r.stdout + r.stderr


def test_parse_default_is_empty_and_no_install_conflicts_with_the_flag():
    import subprocess
    base = {"PATH": "/usr/bin:/bin", "AGENT_BOOTSTRAP_LIB_ONLY": "1"}
    r = subprocess.run(["bash", "-c", f'source "{BOOTSTRAP}"\nab_parse_args nb --runtime claude\necho "REF=[$SANDBOX_REF]"'],
                       capture_output=True, text=True, env=base)
    assert "REF=[]" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(["bash", "-c", f'source "{BOOTSTRAP}"\nab_parse_args nb --runtime claude --no-install --sandbox-build-ref dev'],
                       capture_output=True, text=True, env=base)
    assert r.returncode == 2 and "--sandbox-build-ref" in r.stderr


# ---- fold 2 item 5: mutants nothing pinned ----
def test_clone_argv_has_double_dash_immediately_before_the_source(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    proc = run_lib(env, install_body(home, src))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    clone = [ln for ln in (tmp_path / "git.log").read_text().splitlines() if ln.startswith("git clone ")]
    assert len(clone) == 1 and f" -- {src} " in clone[0], clone


def test_option_looking_ref_is_refused_with_the_guards_own_message(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path)
    proc = run_lib(env, install_body(home, src, 'SANDBOX_REF="-x"'))
    assert _rc(proc) != 0
    assert "looks like an option, not a ref" in proc.stderr
    assert not (tmp_path / "install.log").exists()


def test_parse_refuses_an_option_looking_ref_before_anything_is_created(tmp_path):
    env, home = make_env(tmp_path)
    proc = run_lib(env, 'ab_parse_args nb --runtime claude --sandbox-build-ref -x; echo "PARSED"')
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "looks like an option, not a ref" in proc.stderr
    assert "PARSED" not in proc.stdout
    assert not (tmp_path / "sudo.log").exists()
