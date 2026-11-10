"""Folder-name independence, tools (S6, S7, S8). REVISION 2: `engram` first, then `engram-alpha`.

  S6 tools/rc-upgrade-verify.py REPO: ENGRAM_REPO overrides; else the first existing of
     ~/engram, ~/engram-alpha, ~/src/engram, ~/src/engram-alpha, ~/GitHub/engram, ~/GitHub/engram-alpha.
     None exists -> exit with the "could not locate the engram checkout" text, Tried: list led by ~/engram.
  S7 agent-bootstrap clone destination: an existing $USER_HOME/engram or $USER_HOME/engram-alpha is
     resolved (engram first); a NEW seat's clone is created as $USER_HOME/engram.
  S8 agent-bootstrap shared source default: $SHARED_ROOT/engram if a directory, else
     $SHARED_ROOT/engram-alpha, else empty. ENGRAM_ALPHA_SOURCE still overrides.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agent_bootstrap_install_4097 import (  # noqa: E402
    BOOTSTRAP, SRC, _rc, install_body, make_env, make_source, run_lib,
)

REPO = Path(__file__).resolve().parent.parent
RC = REPO / "tools" / "rc-upgrade-verify.py"


# ---- S6 ---------------------------------------------------------------------

_RC_DRIVER = r"""
import os, sys
src = open(sys.argv[1]).read().split("\ndef bank(")[0]
sys.path.append(sys.argv[2])
g = {"__name__": "rcv_under_test", "__file__": sys.argv[1]}
exec(compile(src, sys.argv[1], "exec"), g)
print("REPO=" + str(g["REPO"]))
"""


def _rc_repo(tmp_path, dirs, extra_env=None):
    home = tmp_path / "home"
    home.mkdir()
    for d in dirs:
        (home / d).mkdir(parents=True)
    eh = tmp_path / "eh"
    eh.mkdir()
    (eh / "knowledge.db").write_text("")
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "ENGRAM_HOME": str(eh),
           "ENGRAM_PY": sys.executable}
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, "-c", _RC_DRIVER, str(RC), str(REPO)],
                       capture_output=True, text=True, env=env, timeout=60)
    return p, home


def _repo_of(p):
    m = re.search(r"^REPO=(.*)$", p.stdout, re.M)
    assert m, p.stdout + p.stderr
    return m.group(1)


def test_S6_only_engram(tmp_path):
    p, home = _rc_repo(tmp_path, ["engram"])
    assert _repo_of(p) == str(home / "engram")


def test_S6_only_alpha(tmp_path):
    p, home = _rc_repo(tmp_path, ["engram-alpha"])
    assert _repo_of(p) == str(home / "engram-alpha")


def test_S6_both_engram_first(tmp_path):
    p, home = _rc_repo(tmp_path, ["engram", "engram-alpha"])
    assert _repo_of(p) == str(home / "engram")


def test_S6_github_new_name(tmp_path):
    p, home = _rc_repo(tmp_path, ["GitHub/engram"])
    assert _repo_of(p) == str(home / "GitHub" / "engram")


def test_S6_src_new_name_beats_github_alpha(tmp_path):
    p, home = _rc_repo(tmp_path, ["src/engram", "GitHub/engram-alpha"])
    assert _repo_of(p) == str(home / "src" / "engram")


def test_S6_env_override_wins(tmp_path):
    o = tmp_path / "override"
    o.mkdir()
    p, home = _rc_repo(tmp_path, ["engram"], {"ENGRAM_REPO": str(o)})
    assert _repo_of(p) == str(o)


def test_S6_neither_fails_naming_engram_default(tmp_path):
    p, home = _rc_repo(tmp_path, [])
    assert p.returncode != 0
    out = p.stdout + p.stderr
    assert "could not locate the engram checkout" in out
    assert f"- {home}/engram\n" in out + "\n"
    assert out.index(f"{home}/engram\n") < out.index(f"{home}/engram-alpha")


# ---- S7 ---------------------------------------------------------------------

def _clone_with(tmp_path, pre_dirs):
    env, home = make_env(tmp_path)
    for d in pre_dirs:
        (home / d).mkdir()
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    proc = run_lib(env, install_body(home, src))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    return home


def test_S7_new_seat_clone_is_engram(tmp_path):
    home = _clone_with(tmp_path, [])
    assert (home / "engram" / "tools" / "install.sh").exists()
    assert not (home / "engram-alpha").exists()


def test_S7_existing_empty_alpha_is_reused(tmp_path):
    home = _clone_with(tmp_path, ["engram-alpha"])
    assert (home / "engram-alpha" / "tools" / "install.sh").exists()
    assert not (home / "engram").exists()


def test_S7_both_exist_engram_first(tmp_path):
    home = _clone_with(tmp_path, ["engram", "engram-alpha"])
    assert (home / "engram" / "tools" / "install.sh").exists()
    assert not (home / "engram-alpha" / "tools").exists()


def test_S7_install_call_and_note_name_the_resolved_clone(tmp_path):
    env, home = make_env(tmp_path)
    src = make_source(tmp_path, tags={"v0.1.0": "c2"})
    proc = run_lib(env, install_body(home, src))
    assert _rc(proc) == 0, proc.stdout + proc.stderr
    call = [ln for ln in (tmp_path / "sudo.log").read_text().splitlines() if "install.sh" in ln][0]
    assert str(home / "engram" / "tools" / "install.sh") in call


# ---- S8 ---------------------------------------------------------------------

def _default_alpha_source(tmp_path, shared_dirs, env_override=None):
    m = re.search(r'(if \[\[ -z "\$ALPHA_SOURCE" \]\]; then\n.*?\nfi\n)', SRC, re.S)
    assert m, "default-source block not found"
    shared = tmp_path / "shared"
    shared.mkdir()
    for d in shared_dirs:
        (shared / d).mkdir()
    script = f'SHARED_ROOT="{shared}"\nALPHA_SOURCE=""\n{m.group(1)}echo "AS=[$ALPHA_SOURCE]"\n'
    env = {"PATH": os.environ["PATH"]}
    if env_override:
        env["ENGRAM_ALPHA_SOURCE"] = env_override
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    return re.search(r"^AS=\[(.*)\]$", p.stdout, re.M).group(1), shared


def test_S8_only_engram(tmp_path):
    got, shared = _default_alpha_source(tmp_path, ["engram"])
    assert got == str(shared / "engram")


def test_S8_only_alpha(tmp_path):
    got, shared = _default_alpha_source(tmp_path, ["engram-alpha"])
    assert got == str(shared / "engram-alpha")


def test_S8_both_engram_first(tmp_path):
    got, shared = _default_alpha_source(tmp_path, ["engram", "engram-alpha"])
    assert got == str(shared / "engram")


def test_S8_neither_empty(tmp_path):
    got, _ = _default_alpha_source(tmp_path, [])
    assert got == ""


def test_S8_env_override_wins(tmp_path):
    o = tmp_path / "ov"
    o.mkdir()
    got, _ = _default_alpha_source(tmp_path, ["engram"], env_override=str(o))
    assert got == str(o)
