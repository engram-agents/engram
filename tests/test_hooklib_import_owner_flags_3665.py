"""Contract tests for `_hooklib.import_owner_flags` (U6b stage 1, #3665, part of #2652).

Written BEFORE the hoist (contract-first; project CLAUDE.md, "Contract-first tests").
The body used to live twice, byte-similar, as `_import_owner_flags()` in
engram-stop-hook.py and engram-user-identity-hook.py. Contract (identical to the stop
hook's body before the hoist):

  * pop ENGRAM_RUNTIME_DIR, resolve the runtime dir for "engramd_owner_flags.py", restore
    the explicit value; candidates = [explicit if set] + [resolved];
  * per candidate: put it at sys.path[0] unless already on sys.path, import the module;
    any `Exception` is remembered, not raised;
  * afterwards: nothing failed, or the last failure is a ModuleNotFoundError for this very
    module -> ImportError("engramd_owner_flags.py not found under the runtime dir");
    otherwise re-raise the LAST failure with its own class;
  * KNOWN LIMITS (pinned): (a) SystemExit from an explicit-dir copy propagates at once and
    the good resolved copy is not tried; (b) a resolved dir already on sys.path behind a bad
    explicit copy is not retried;
  * process-global effects stay inside the function: ENGRAM_RUNTIME_DIR is exactly as it
    was after any call, success or raise.

Every cell names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if str(_HOOKS_SRC) not in sys.path:
    sys.path.insert(0, str(_HOOKS_SRC))

import _hooklib  # noqa: E402

GOOD = "MARK = 'good'\n"
GOOD_OTHER = "MARK = 'other-good'\n"
NAME = "engramd_owner_flags.py"
NOT_FOUND_MSG = "engramd_owner_flags.py not found under the runtime dir"


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="hlib-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def cands(home, monkeypatch):
    """Two candidate dirs. Returns build(explicit_src, resolved_src) -> (exp, res);
    None = the dir exists but holds no copy; explicit_src=False = ENGRAM_RUNTIME_DIR unset.
    The resolved dir is injected through `_hooklib.resolve_runtime_dir`; the repo's real src
    dir is taken off sys.path (else a real copy hides behind both candidates)."""
    def build(explicit_src, resolved_src):
        exp, res = home / "cand-explicit", home / "cand-resolved"
        exp.mkdir()
        res.mkdir()
        if explicit_src:
            (exp / NAME).write_text(explicit_src)
        if resolved_src:
            (res / NAME).write_text(resolved_src)
        if explicit_src is False:
            monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
        else:
            monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(exp))
        monkeypatch.delitem(sys.modules, "engramd_owner_flags", raising=False)
        real = os.path.realpath(_SRC_ENGRAM)
        monkeypatch.setattr(sys, "path", [p for p in sys.path if os.path.realpath(p) != real])
        monkeypatch.setattr(_hooklib, "resolve_runtime_dir", lambda h, s: str(res))
        return exp, res
    return build


def _call(home):
    return _hooklib.import_owner_flags(str(home))


def test_good_single_candidate(home, cands):
    """No explicit dir, one good resolved copy -> that module. Mutant: drop the resolved
    candidate -> ImportError."""
    exp, res = cands(False, GOOD)
    assert _call(home).MARK == "good"
    assert os.path.dirname(sys.modules["engramd_owner_flags"].__file__) == str(res)


def test_bad_explicit_then_good_resolved_returns_the_good_module(home, cands):
    """Mutant M1: catch only ImportError per candidate -> the explicit TypeError escapes the
    loop and the good resolved copy is never tried."""
    cands("raise TypeError('union on old python')\n", GOOD)
    assert _call(home).MARK == "good"


def test_explicit_good_wins_over_resolved_good(home, cands):
    """Candidate order: explicit first. Mutant: reverse the order -> 'good'."""
    cands(GOOD_OTHER, GOOD)
    assert _call(home).MARK == "other-good"


def test_both_bad_raises_the_last_failure_in_its_own_class(home, cands):
    """TypeError (explicit) then SyntaxError (resolved) -> SyntaxError. Mutant M2: always
    raise ImportError, or raise the first failure -> red."""
    cands("raise TypeError('x')\n", "def (:\n")
    with pytest.raises(SyntaxError):
        _call(home)


def test_nothing_anywhere_is_an_importerror_with_the_exact_message(home, cands):
    """Mutant: a different message, or re-raising the ModuleNotFoundError itself -> red."""
    cands(None, None)
    with pytest.raises(ImportError) as ei:
        _call(home)
    assert type(ei.value) is ImportError
    assert str(ei.value) == NOT_FOUND_MSG


def test_modulenotfound_for_a_different_name_is_reraised_not_relabelled(home, cands):
    """A copy that exists but fails on `import <other missing module>` -> that
    ModuleNotFoundError (name != engramd_owner_flags). Mutant M2: always ImportError(not
    found) -> the type/name assertions go red."""
    cands(None, "import some_other_missing_mod_3665\n")
    with pytest.raises(ModuleNotFoundError) as ei:
        _call(home)
    assert ei.value.name == "some_other_missing_mod_3665"


def test_limit_a_systemexit_from_explicit_propagates_and_resolved_is_not_tried(home, cands):
    """KNOWN LIMIT (a). Mutant M4: swallow SystemExit per candidate -> the good resolved copy
    is returned and this cell goes red."""
    cands("raise SystemExit(3)\n", GOOD)
    with pytest.raises(SystemExit):
        _call(home)
    assert "engramd_owner_flags" not in sys.modules


def test_limit_b_resolved_dir_already_on_sys_path_behind_a_bad_explicit_is_not_retried(
        home, cands, monkeypatch):
    """KNOWN LIMIT (b), pinned as it stands: the resolved dir is already on sys.path (so it
    is not re-inserted at index 0) but sits behind the explicit dir, which the first candidate
    put at index 0; the bad explicit copy still wins the lookup, so the second attempt fails
    the same way -> the failure is raised, the good copy is not used."""
    exp, res = cands("raise TypeError('bad explicit')\n", GOOD)
    monkeypatch.setattr(sys, "path", list(sys.path) + [str(res)])
    with pytest.raises(TypeError):
        _call(home)


@pytest.mark.parametrize("env_before", ["set", "absent"])
@pytest.mark.parametrize("outcome", ["success", "systemexit", "importerror", "badcopy"])
def test_env_is_exactly_restored_after_success_and_after_each_raise(
        env_before, outcome, home, cands):
    """Mutant M3: pop without restoring -> the 'set' cells go red. The 'absent' cells guard
    a mutant that leaves the variable set on the way out."""
    bodies = {"success": GOOD, "systemexit": "raise SystemExit(1)\n",
              "importerror": None, "badcopy": "raise TypeError('x')\n"}
    body = bodies[outcome]
    if env_before == "set":
        exp, res = cands(body, GOOD if outcome == "success" else None)
    else:
        exp, res = cands(False, body)
    before = os.environ.get("ENGRAM_RUNTIME_DIR")
    assert (before is None) == (env_before == "absent")
    expected = {"success": None, "systemexit": SystemExit, "importerror": ImportError,
                "badcopy": TypeError}[outcome]
    if expected is None:
        _call(home)
    else:  # the outcome really happened (not a vacuous pass on some other error)
        with pytest.raises(expected):
            _call(home)
    assert os.environ.get("ENGRAM_RUNTIME_DIR") == before


def test_env_is_restored_even_when_the_resolver_raises(home, cands, monkeypatch):
    """The restore is in a finally. Mutant: restore outside the finally -> red."""
    exp, _ = cands(GOOD, GOOD)

    def boom(h, s):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(_hooklib, "resolve_runtime_dir", boom)
    with pytest.raises(RuntimeError):
        _call(home)
    assert os.environ["ENGRAM_RUNTIME_DIR"] == str(exp)


def test_resolver_sees_no_override_the_given_home_and_the_right_sentinel(home, cands, monkeypatch):
    """The resolver must see ENGRAM_RUNTIME_DIR absent (that is why it is popped). Mutant:
    do not pop -> red."""
    exp, res = cands(GOOD_OTHER, GOOD)
    seen = []

    def spy(h, s):
        seen.append((h, s, os.environ.get("ENGRAM_RUNTIME_DIR")))
        return str(res)
    monkeypatch.setattr(_hooklib, "resolve_runtime_dir", spy)
    _call(home)
    assert seen == [(str(home), NAME, None)]


def test_sys_path_gets_the_candidate_at_index_zero_only_when_not_already_present(home, cands, monkeypatch):
    """Mutant: always insert (duplicate), or append instead of index 0."""
    exp, res = cands(GOOD, GOOD)
    n = sys.path.count(str(exp))
    _call(home)
    assert sys.path[0] == str(exp)
    assert sys.path.count(str(exp)) == n + 1
    monkeypatch.delitem(sys.modules, "engramd_owner_flags", raising=False)
    _call(home)
    assert sys.path.count(str(exp)) == n + 1


def test_an_already_present_dir_is_not_moved_to_the_front(home, cands, monkeypatch):
    """Present anywhere -> left where it is. Mutant: unconditional insert(0)."""
    exp, res = cands(False, GOOD)
    monkeypatch.setattr(sys, "path", list(sys.path) + [str(res)])
    _call(home)
    assert sys.path[0] != str(res)
    assert sys.path.count(str(res)) == 1


# --- the two hooks keep their own name as a thin alias ---------------------------------

def _load(name, filename, home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location(name, str(_HOOKS_SRC / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HOOKS = [("engram_stop_hook_3665", "engram-stop-hook.py"),
         ("engram_user_identity_hook_3665", "engram-user-identity-hook.py")]


@pytest.mark.parametrize("name,filename", HOOKS)
def test_hook_alias_has_the_hoisted_behaviour(name, filename, home, cands, monkeypatch):
    """Each hook's `_import_owner_flags` gives the hoisted result (bad explicit, good
    resolved -> good) and the hoisted not-found message, calling the resolver with the HOOK'S
    ENGRAM_HOME. Mutant M5: a hook carries its own body again."""
    mod = _load(name, filename, home, monkeypatch)
    cands("raise TypeError('x')\n", GOOD)
    path_before = list(sys.path)
    assert mod._import_owner_flags().MARK == "good"
    monkeypatch.setattr(sys, "path", path_before)  # drop the dirs the first call inserted
    seen = []
    monkeypatch.setattr(_hooklib, "resolve_runtime_dir",
                        lambda h, s: seen.append(h) or "/nonexistent-3665")
    monkeypatch.delitem(sys.modules, "engramd_owner_flags", raising=False)
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    with pytest.raises(ImportError) as ei:
        mod._import_owner_flags()
    assert str(ei.value) == NOT_FOUND_MSG
    assert seen == [mod.ENGRAM_HOME]


@pytest.mark.parametrize("name,filename", HOOKS)
def test_hook_alias_delegates_to_the_hoisted_function(name, filename, home, monkeypatch):
    """`_hooklib.import_owner_flags` is replaced BEFORE the hook loads; the alias must reach
    that function with the hook's ENGRAM_HOME. Mutant M5 (bypass): a re-inlined body never
    calls it -> red."""
    sentinel = object()
    calls = []
    monkeypatch.setattr(_hooklib, "import_owner_flags", lambda h: calls.append(h) or sentinel)
    mod = _load(name, filename, home, monkeypatch)
    assert mod._import_owner_flags() is sentinel
    assert calls == [mod.ENGRAM_HOME]


class _FlagsOn:
    @staticmethod
    def flag_on(key, path):
        return True


class _FlagsOff:
    @staticmethod
    def flag_on(key, path):
        return False


@pytest.mark.parametrize("name,filename", HOOKS)
def test_owner_reads_enabled_calls_the_patchable_hook_attribute(name, filename, home, monkeypatch):
    """Existing tests do `monkeypatch.setattr(mod, "_import_owner_flags", ...)`; the flag
    reader must still go through that attribute. Mutant: `_owner_reads_enabled` calls the
    hoisted function directly -> the patched fakes are ignored -> red."""
    mod = _load(name, filename, home, monkeypatch)
    monkeypatch.setattr(mod, "_import_owner_flags", lambda: _FlagsOn)
    assert mod._owner_reads_enabled() is True
    monkeypatch.setattr(mod, "_import_owner_flags", lambda: _FlagsOff)
    assert mod._owner_reads_enabled() is False
