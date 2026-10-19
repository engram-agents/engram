"""The bootstrap.py lookup that engram-first-session prints, run exactly as printed (#3863).

The Step 0.5 bash fence in the engram-first-session skill finds bootstrap.py via
CLAUDE_PLUGIN_ROOT, then the plugin cache, then ``${ENGRAM_HOME:-$HOME/.engram}``. This test
extracts that fence, cuts it after the ERROR line (the python that follows is not run), and
runs it with bash in a fresh temp HOME. It asserts which file the lookup resolves:

  C1  ENGRAM_HOME unset            -> $HOME/.engram/bootstrap.py
  C2  ENGRAM_HOME has a space      -> that directory's bootstrap.py (ENGRAM_HOME is used, and a
                                      spaced path survives the lookup; bash does not word-split a
                                      plain VAR=value assignment, so the quotes on that one line
                                      are not what this cell pins)
  C3  nothing exists anywhere      -> exit 1 and "ERROR: cannot locate bootstrap.py"
  C3b nothing exists, ENGRAM_HOME has a space (the directory exists, no bootstrap.py in it)
                                   -> exit 1 and the ERROR line (pins the quoted "$BOOTSTRAP" in
                                      the last -f test: unquoted, a spaced path makes the test
                                      error instead of return false, so no ERROR line prints)
  C5  CLAUDE_PLUGIN_ROOT and ENGRAM_HOME both have bootstrap.py -> the plugin root wins
  C6  plugin cache and ENGRAM_HOME both have bootstrap.py (no plugin root) -> the cache wins
  C7  CLAUDE_PLUGIN_ROOT has a space and holds no bootstrap.py, ENGRAM_HOME has one -> falls back
                                   to ENGRAM_HOME with no "binary operator expected" (pins the
                                      quoted "$BOOTSTRAP" in the -f test on the ENGRAM_HOME line)

It depends on no git history, no pinned commit, no line numbers, no network, and never reads
the real ~/.engram (HOME is a temp dir, the env is built from scratch).
"""

import os
import subprocess
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parent.parent
    / "src" / "engram" / "skills" / "claude" / "engram-first-session" / "SKILL.md"
)
NEEDLE = 'BOOTSTRAP="${CLAUDE_PLUGIN_ROOT:+'
ERROR_MARK = "ERROR: cannot locate bootstrap.py"


def _fence_bodies(text):
    """Yield the body of every fenced code block (``` ... ```), as a string."""
    body, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if inside:
                yield "\n".join(body)
                body = []
            inside = not inside
            continue
        if inside:
            body.append(line)


def _lookup_script():
    found = [b for b in _fence_bodies(SKILL.read_text()) if NEEDLE in b]
    assert len(found) == 1, (
        f"expected exactly ONE bash fence containing {NEEDLE!r} in {SKILL.name}, found {len(found)}"
    )
    lines = found[0].splitlines()
    cut = [i for i, ln in enumerate(lines) if ERROR_MARK in ln]
    assert cut, f"the fence has no line containing {ERROR_MARK!r}"
    return "\n".join(lines[: cut[0] + 1]) + '\necho "BOOTSTRAP=$BOOTSTRAP"\n'


def _run(tmp_path, engram_home=None, plugin_root=None):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}  # CLAUDE_PLUGIN_ROOT only if given
    if engram_home is not None:
        env["ENGRAM_HOME"] = str(engram_home)
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    r = subprocess.run(
        ["bash", "-c", _lookup_script()], env=env, capture_output=True, text=True, cwd=tmp_path
    )
    return r, home


def test_the_lookup_fence_is_unique_in_the_skill():
    _lookup_script()


def test_c1_engram_home_unset_resolves_the_dot_engram_bootstrap(tmp_path):
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    (home / ".engram" / "bootstrap.py").write_text("")
    assert not (home / ".claude" / "plugins" / "cache" / "engram" / "bootstrap.py").exists()
    r, home = _run_existing(tmp_path, home)
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"BOOTSTRAP={home}/.engram/bootstrap.py" in r.stdout, r.stdout


def test_c2_engram_home_with_a_space_resolves_that_bootstrap(tmp_path):
    spaced = tmp_path / "my engram home"
    spaced.mkdir()
    (spaced / "bootstrap.py").write_text("")
    r, home = _run(tmp_path, engram_home=spaced)
    assert not (home / ".engram").exists()
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"BOOTSTRAP={spaced}/bootstrap.py" in r.stdout, r.stdout


def test_c3_nothing_anywhere_exits_1_with_the_error_line(tmp_path):
    r, _ = _run(tmp_path)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert ERROR_MARK in r.stdout, r.stdout


def test_c3b_nothing_exists_and_engram_home_has_a_space_still_prints_the_error(tmp_path):
    spaced = tmp_path / "my engram home"
    spaced.mkdir()  # exists, but holds no bootstrap.py
    r, _ = _run(tmp_path, engram_home=spaced)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert ERROR_MARK in r.stdout, (r.stdout, r.stderr)


def test_c5_plugin_root_wins_over_engram_home(tmp_path):
    root = tmp_path / "plugin-root"
    root.mkdir()
    (root / "bootstrap.py").write_text("")
    eh = tmp_path / "eh"
    eh.mkdir()
    (eh / "bootstrap.py").write_text("")
    r, _ = _run(tmp_path, engram_home=eh, plugin_root=root)
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"BOOTSTRAP={root}/bootstrap.py" in r.stdout, r.stdout


def test_c6_plugin_cache_wins_over_engram_home(tmp_path):
    home = tmp_path / "home"
    cache = home / ".claude" / "plugins" / "cache" / "engram"
    cache.mkdir(parents=True)
    (cache / "bootstrap.py").write_text("")
    eh = tmp_path / "eh"
    eh.mkdir()
    (eh / "bootstrap.py").write_text("")
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "ENGRAM_HOME": str(eh)}
    r = subprocess.run(
        ["bash", "-c", _lookup_script()], env=env, capture_output=True, text=True, cwd=tmp_path
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"BOOTSTRAP={cache}/bootstrap.py" in r.stdout, r.stdout


def test_c7_spaced_plugin_root_without_bootstrap_falls_back_to_engram_home(tmp_path):
    root = tmp_path / "plugin root"
    root.mkdir()  # exists, but holds no bootstrap.py
    eh = tmp_path / "eh"
    eh.mkdir()
    (eh / "bootstrap.py").write_text("")
    r, _ = _run(tmp_path, engram_home=eh, plugin_root=root)
    assert "binary operator expected" not in r.stderr, r.stderr
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert f"BOOTSTRAP={eh}/bootstrap.py" in r.stdout, (r.stdout, r.stderr)


def _run_existing(tmp_path, home):
    """Run the lookup in an already-prepared HOME (ENGRAM_HOME unset)."""
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    r = subprocess.run(
        ["bash", "-c", _lookup_script()], env=env, capture_output=True, text=True, cwd=tmp_path
    )
    return r, home
