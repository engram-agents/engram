"""engram-alpha#3983, shell spelling: start-engramd-owner.sh bounds its sibling lookups.

``_find_io_helper`` (looks for ``_engramd_owner_hook_io.py``) and ``_find_plugin_root``
(looks for ``engramd_owner_flags.py``) used to ``dirname``-walk to ``/``.  They now search
only through ``_bounded_dirs``: the script's own directory up to and INCLUDING its tree
root (the nearest ancestor holding ``engram_core.py``), never above it; with no
``engram_core.py`` anywhere above, only the own directory and its parent.
``CLAUDE_PLUGIN_ROOT`` stays the first candidate.

Each cell runs the REAL shipped function bodies (extracted from the script) in bash,
from a wrapper script placed in a tmp layout, so ``BASH_SOURCE[0]`` is the fixture:

  (a) target inside the tree: found.
  (b) DISCRIMINATING: target planted only ABOVE the tree root, CLAUDE_PLUGIN_ROOT unset:
      must NOT be found (an unbounded walk finds it).
  (c) CLAUDE_PLUGIN_ROOT checked first.
  (e) no engram_core.py anywhere: parent found, grandparent NOT.

Plus a bash 3.2 text check: the script uses no bash-4-only construct (macOS seats ship 3.2).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude" / "start-engramd-owner.sh"
IO = "_engramd_owner_hook_io.py"
FLAGS = "engramd_owner_flags.py"
MARKER = "engram_core.py"

FUNCS = ("_bounded_dirs", "_find_io_helper", "_find_plugin_root")


def _extract(name: str) -> str:
    text = SCRIPT.read_text()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name} not found in {SCRIPT}"
    return m.group(0)


def _layout(tmp: Path, flavour: str, with_marker: bool = True):
    if flavour == "installed":
        root = tmp / "stale_home" / "plugin"
        own = root / "hooks"
    else:
        root = tmp / "repo" / "src" / "engram"
        own = root / "hooks" / "claude"
    own.mkdir(parents=True)
    if with_marker:
        (root / MARKER).write_text("")
    wrapper = own / "start-engramd-owner.sh"
    wrapper.write_text("#!/usr/bin/env bash\n" + "\n".join(_extract(n) for n in FUNCS) + '\n"$@"\n')
    return root, own, wrapper


def _run(wrapper: Path, func: str, plugin_root: Path | None = None) -> str | None:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    proc = subprocess.run(["bash", str(wrapper), func], capture_output=True, text=True, env=env, timeout=30)
    if proc.returncode == 1:
        assert proc.stdout == "", proc.stdout
        return None
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _put(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


FLAVOURS = ["installed", "source"]


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_flags_found_at_the_tree_root(flavour, tmp_path):
    root, own, w = _layout(tmp_path, flavour)
    _put(root / FLAGS)
    _put(root.parent / FLAGS)  # a decoy above the root must not matter
    assert _run(w, "_find_plugin_root") == str(root)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_io_helper_found_in_the_tree(flavour, tmp_path):
    root, own, w = _layout(tmp_path, flavour)
    want = _put(own / IO)
    _put(root.parent / IO)
    assert _run(w, "_find_io_helper") == str(want)
    want.unlink()
    want = _put(root / "hooks" / IO)  # flattened-bundle shape: <dir>/hooks/<file>, reached from the root
    assert _run(w, "_find_io_helper") == str(want)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_b_flags_only_above_tree_root_is_absent(flavour, tmp_path):
    root, own, w = _layout(tmp_path, flavour)
    _put(root.parent / FLAGS)
    assert _run(w, "_find_plugin_root") is None, "walked above the tree root"


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("shape", ["direct", "hooks_subdir"])
def test_b_io_helper_only_above_tree_root_is_absent(flavour, shape, tmp_path):
    root, own, w = _layout(tmp_path, flavour)
    above = root.parent
    _put(above / IO if shape == "direct" else above / "hooks" / IO)
    assert _run(w, "_find_io_helper") is None, "walked above the tree root"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_b_stale_scatter_ancestor_far_above_is_absent(flavour, tmp_path):
    """The reported hazard: a scatter-era ENGRAM home folder several levels above the plugin root."""
    root, own, w = _layout(tmp_path, flavour)
    _put(tmp_path / FLAGS)
    _put(tmp_path / IO)
    assert _run(w, "_find_plugin_root") is None
    assert _run(w, "_find_io_helper") is None


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_c_plugin_root_is_checked_first(flavour, tmp_path):
    root, own, w = _layout(tmp_path, flavour)
    pr = tmp_path / "pluginroot"
    _put(pr / FLAGS)
    _put(pr / "hooks" / IO)
    _put(root / FLAGS)
    _put(own / IO)
    assert _run(w, "_find_plugin_root", plugin_root=pr) == str(pr)
    assert _run(w, "_find_io_helper", plugin_root=pr) == str(pr / "hooks" / IO)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_e_no_marker_searches_own_dir_and_parent_only(flavour, tmp_path):
    root, own, w = _layout(tmp_path, flavour, with_marker=False)
    parent = own.parent
    _put(parent / FLAGS)
    assert _run(w, "_find_plugin_root") == str(parent)
    (parent / FLAGS).unlink()
    _put(parent.parent / FLAGS)  # grandparent only
    assert _run(w, "_find_plugin_root") is None, "grandparent must NOT be searched"
    _put(parent / IO)
    assert _run(w, "_find_io_helper") == str(parent / IO)
    (parent / IO).unlink()
    _put(parent.parent / IO)
    assert _run(w, "_find_io_helper") is None


def test_bounded_dirs_lists_own_dir_through_the_root_nearest_first(tmp_path):
    root, own, w = _layout(tmp_path, "source")
    out = _run(w, "_bounded_dirs")
    assert out.splitlines() == [str(own), str(own.parent), str(root)]


def test_the_script_uses_no_bash4_only_construct():
    """macOS seats ship bash 3.2: no associative arrays, mapfile/readarray, case-modifying
    expansions, ``&>>``, ``|&``, coproc, namerefs, ``[[ -v``, or bash-5 variables."""
    banned = re.compile(r"declare -A|local -A|mapfile|readarray|\$\{[A-Za-z_]+(?:,,|\^\^|@[UuLl])"
                        r"|&>>|\|&|\bcoproc\b|local -n|declare -n|\[\[ -v|EPOCHSECONDS|BASHPID|wait -n|SRANDOM")
    for n, line in enumerate(SCRIPT.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        assert not banned.search(line), f"{SCRIPT.name}:{n}: bash-4+ construct: {line.strip()}"
