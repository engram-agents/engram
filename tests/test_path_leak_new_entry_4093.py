"""engram-alpha#4093 -- the sys.path leak guard also catches a NEW src/engram
entry at any position, not only a changed `sys.path[0]`.

The contract these cells state. At a module's teardown, the guard reports a
`sys.path` leak when EITHER:

    1. (unchanged) `sys.path[0]` is now inside `src/engram` and differs from
       `sys.path[0]` at setup; OR
    2. (NEW) `sys.path` holds at least one entry that is inside `src/engram`
       (abspath equal to SRC_ENGRAM or starting with SRC_ENGRAM + os.sep) and
       whose abspath was NOT present anywhere in `sys.path` at setup. Position
       does not matter.

It does NOT report: an added entry whose abspath was already present at setup (a
duplicate re-insert, coverage limit (b), deliberately kept); an added entry
outside `src/engram`; an entry like `<repo>/src/engram_other` (prefix without a
separator). Non-str `sys.path` entries are ignored by rule 2. Limits (a)
(import-time leaks) and (b) are unchanged. The report for rule 2 names the count and up to three of the new entries, sorted. When both
rules fire, ONE message names both (the single RuntimeError per module stays).
A module caught by rule 2 is added to `_PATH_LEAKS` like rule 1, and the session
summary line `#4023 sys.path[0] leaks: N module(s): ...` keeps its format.

Why: a hook loaded in process without the shared window can leave
`src/engram/hooks/claude` somewhere behind a tmp runtime dir that sits in front
(the forum-monitor shape). The `[0]` check alone is silent on that.

Unit cells (U*) call `conftest._path_leak_problem(path_before)` with `sys.path`
swapped by monkeypatch. End-to-end cells (E*) run a real pytest in a subprocess
over planted modules, the pattern of tests/test_syspath_polluters_4023.py.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
TIMEOUT_S = 600


def _src():
    import conftest
    return conftest.SRC_ENGRAM


def _problem(monkeypatch, before, after):
    import conftest
    monkeypatch.setattr(sys, "path", list(after))
    return conftest._path_leak_problem(list(before))


# --- unit cells -------------------------------------------------------------------

def test_u1_forum_monitor_shape_non_src_front_and_new_src_entry_behind(monkeypatch, tmp_path):
    """U1. A tmp dir outside src in front, a new src/engram/hooks/claude behind."""
    src = _src()
    hook = os.path.join(src, "hooks", "claude")
    before = [str(REPO_ROOT), src, "/x/site-packages"]
    after = [str(tmp_path)] + before + [hook]
    problem = _problem(monkeypatch, before, after)
    assert problem is not None
    assert hook in problem, problem


def test_u2_duplicate_reinsert_of_an_entry_present_at_setup_is_not_reported(monkeypatch):
    """U2. Limit (b), deliberately kept."""
    src = _src()
    before = [str(REPO_ROOT), src, "/x/site-packages"]
    assert _problem(monkeypatch, before, before + [src, src]) is None


def test_u3_new_entry_outside_src_engram_at_any_position_is_not_reported(monkeypatch, tmp_path):
    src = _src()
    before = [str(REPO_ROOT), src]
    other = str(tmp_path / "elsewhere")
    assert _problem(monkeypatch, before, [other] + before) is None
    assert _problem(monkeypatch, before, before + [other]) is None


def test_u4_old_front_case_is_still_reported_by_rule_1(monkeypatch):
    """U4. The added entry already existed at setup (so rule 2 alone is silent),
    but it is now in front and the front changed: rule 1 reports it."""
    src = _src()
    tools = os.path.join(src, "tools")
    before = ["/x/site-packages", tools, str(REPO_ROOT)]
    assert _problem(monkeypatch, before, [tools] + before) is not None


def test_u5_added_entry_with_the_same_abspath_as_a_present_one_is_not_reported(monkeypatch):
    """U5. Normalisation: `<src>/tools/..` is `<src>`."""
    src = _src()
    before = [str(REPO_ROOT), src]
    assert _problem(monkeypatch, before, before + [os.path.join(src, "tools", "..")]) is None


def test_u6_prefix_without_separator_is_not_inside_src_engram(monkeypatch):
    """U6. `<repo>/src/engram_other` is not under `<repo>/src/engram`."""
    src = _src()
    before = [str(REPO_ROOT), src]
    assert _problem(monkeypatch, before, before + [src + "_other"]) is None


def test_u7_non_str_entries_after_setup_are_ignored(monkeypatch):
    """U7. Non-str `sys.path` entries are ignored by rule 2 (no exception)."""
    src = _src()
    before = [str(REPO_ROOT), src]
    assert _problem(monkeypatch, before, before + [None, 5]) is None


def test_u8_non_str_front_entry_is_ignored(monkeypatch):
    """U8. A `None` `sys.path[0]` is ignored by rule 1 too (no exception). Rule 1 still calls
    `os.path.abspath` on any other front entry, so a non-`None` non-str front is not covered."""
    src = _src()
    before = [str(REPO_ROOT), src]
    assert _problem(monkeypatch, before, [None] + before) is None


# --- end-to-end cells ---------------------------------------------------------------

_PLANT_FOREIGN_FRONT = textwrap.dedent('''\
    import os, sys

    def test_leaks_behind_a_foreign_front():
        sys.path.append(os.path.join(os.environ["REPO_ROOT_4093"], "src", "engram", "hooks", "claude"))
        sys.path.insert(0, os.environ["FRONT_DIR_4093"])
    ''')

_PLANT_REINSERT = textwrap.dedent('''\
    import os, sys

    def test_reinserts_an_entry_present_at_setup():
        src = os.path.join(os.environ["REPO_ROOT_4093"], "src", "engram")
        assert src in sys.path
        sys.path.append(src)
        sys.path.append(src)
    ''')


def _run_modules(tmp_path, modules):
    d = tmp_path / "planted"
    d.mkdir()
    front = tmp_path / "front_dir"
    front.mkdir()
    for name, body in modules.items():
        (d / name).write_text(body)
    pypath = os.pathsep.join(
        p for p in (str(TESTS_DIR), os.environ.get("PYTHONPATH", "")) if p)
    env = dict(os.environ)
    env.update({"PYTHONPATH": pypath, "REPO_ROOT_4093": str(REPO_ROOT),
                "FRONT_DIR_4093": str(front)})
    # `-p conftest` loads the REAL tests/conftest.py for modules outside tests/.
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(d), "-q", "-p", "no:cacheprovider",
         "-p", "conftest", "--rootdir", str(d)],
        cwd=str(d), env=env, capture_output=True, text=True, timeout=TIMEOUT_S)


def test_e1_new_src_entry_behind_a_foreign_front_errors_and_is_listed(tmp_path):
    proc = _run_modules(tmp_path, {"test_plant_foreign_front.py": _PLANT_FOREIGN_FRONT})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-3000:]
    assert "ERROR at teardown of test_leaks_behind_a_foreign_front" in out, out[-3000:]
    assert "test_plant_foreign_front" in out, out[-3000:]
    lines = [ln for ln in out.splitlines() if ln.startswith("#4023 sys.path[0] leaks:")]
    assert lines == [
        "#4023 sys.path[0] leaks: 1 module(s): test_plant_foreign_front"], out[-3000:]


def test_e2_reinsert_of_an_entry_present_at_setup_is_clean(tmp_path):
    proc = _run_modules(tmp_path, {"test_plant_reinsert.py": _PLANT_REINSERT})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert re.search(r"\b1 passed\b", out), out[-3000:]
    assert "#4023" not in out, out[-3000:]
