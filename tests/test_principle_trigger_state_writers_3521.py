"""Contract-first tests for #3521 row 40, PR 2: the two OTHER writers of
`principle-trigger-state.json` never overwrite an unusable file, and all three
readers classify the file the same way.

PR 1 (#3654) taught the surface hook (and `_hooklib.read_state_json_classified`)
that a corrupt state file is UNKNOWN and is moved aside. Two more code paths
read-modify-write the same file and still treated any parse failure as `{}`:

  * `engram_core._reset_principle_enactments` (MCP server; reached from
    engram_register_exemplar / add_edge / add_task through callers that wrap it
    in a bare `except Exception: pass`): corrupt JSON was read as `{}` and the
    file OVERWRITTEN with one entry; a JSON list raised AttributeError, which
    every caller swallowed, so the reset was silently skipped.
  * `engram-utility-credit-mention-stop.py::_check_enactments` (a Stop hook;
    its caller `credit_mentions` wraps it in `except Exception` -> log): the
    same two failures.

The contract (maintainer rulings, binding):

  1. ONE read rule in all three readers: absent / dict / UNKNOWN. The two hooks
     share the `_hooklib` helper. Core may not import a hook module, so it keeps
     its own copy in `engram_regular_io.py`. Parity is pinned here (P1) and by
     AST equality in test_regular_io_parity_3628.py (P2).
  4. On UNKNOWN, core and the utility hook SKIP the write and print ONE stderr
     line from inside the function itself (so neither caller's swallowing
     `except` can eat it). They never move the file aside (only the per-prompt
     surface hook does) and inject nothing.
  5. A symlinked state path is UNKNOWN, never moved, never written, in BOTH
     classifier copies (`os.lstat` first; `os.replace` would rename the LINK and
     leave the corrupt target behind a `.corrupt` link).

Cells: U1/U2 utility, C1/C2 core, P1 parity, S1 surface hook with a symlink.
"""

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC / "hooks" / "claude"
_SURFACE_PATH = HOOK_DIR / "engram-surface-hook.py"
_UTILITY_PATH = HOOK_DIR / "engram-utility-credit-mention-stop.py"
_UTILITY_REL = "src/engram/hooks/claude/engram-utility-credit-mention-stop.py"
_CORE_REL = "src/engram/engram_core.py"

STATE_NAME = "principle-trigger-state.json"
SIDECAR_NAME = "principle-trigger-state.json.corrupt"
MARKER = "principle-trigger-state"
TRIG = "tr_9041"
PID = "ls_9040"
OTHER = "ls_9042"

REGISTRY = {
    TRIG: [{
        "principle_id": PID,
        "kind": "lesson",
        "claim": "r40 claim text",
        "nudge": "r40 nudge text",
    }],
}
RICH_STATE = {
    PID: {"last_fired_prompt": 1, "strength": 0.7, "enactments": 3, "fires": 4},
    OTHER: {"last_fired_prompt": 2, "strength": 0.9, "enactments": 1, "fires": 6},
}
CORRUPT = b'{"ls_9040": {"last_fired_prompt": 1, "strength": 0.7, "enac'
LIST_BYTES = b'[1, 2, 3]'

# (id, bytes to plant, chmod, reason class the writer must report)
UNKNOWN_CASES = [
    ("corrupt_json", CORRUPT, None, "JSONDecodeError"),
    ("list", LIST_BYTES, None, "NotADict"),
    ("mode_000", json.dumps(RICH_STATE).encode(), 0, "PermissionError"),
]

_COUNTER = 0


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _case_params():
    return [pytest.param(b, m, cls, id=i) for i, b, m, cls in UNKNOWN_CASES]


def _plant(path: Path, data: bytes, mode):
    path.write_bytes(data)
    if mode is not None:
        if _is_root():
            pytest.skip("root bypasses mode 000")
        path.chmod(mode)


def _restore_mode(path: Path):
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load(path: Path, name: str):
    global _COUNTER
    _COUNTER += 1
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    spec = importlib.util.spec_from_file_location(f"{name}_{_COUNTER}", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hooklib():
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    import _hooklib
    return _hooklib


def _core_io():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import engram_regular_io
    return engram_regular_io


def _git(*args):
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args],
                          capture_output=True, text=True)


def _merge_base_text(rel: str) -> str:
    """`rel` as it is at `git merge-base origin/dev HEAD` (NOT the moving
    origin/dev tip). Skips when HEAD is already in dev (nothing to compare)."""
    if _git("rev-parse", "--verify", "origin/dev").returncode != 0:
        pytest.skip("origin/dev not available in this checkout")
    if _git("merge-base", "--is-ancestor", "HEAD", "origin/dev").returncode == 0:
        pytest.skip("HEAD is already contained in origin/dev: no change to compare")
    mb = _git("merge-base", "origin/dev", "HEAD")
    if mb.returncode != 0:
        pytest.skip("no merge-base with origin/dev")
    show = _git("show", f"{mb.stdout.strip()}:{rel}")
    if show.returncode != 0:
        pytest.skip(f"{rel} not present at merge-base")
    return show.stdout


def _marker_lines(text: str):
    return [ln for ln in text.splitlines() if MARKER in ln]


def _assert_one_clean_line(err: str, cls: str, tail: str):
    lines = _marker_lines(err)
    assert len(lines) == 1, err
    line = lines[0]
    assert cls in line, line
    assert "file left untouched" in line and tail in line, line
    assert len(line) < 400 and all(ord(c) >= 32 for c in line), line


# ---------------------------------------------------------------------------
# utility hook
# ---------------------------------------------------------------------------

def _utility(monkeypatch, home: Path, path: Path = _UTILITY_PATH):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return _load(path, "utility_hook_3521r40")


def _util_home(tmp_path: Path, name="home") -> Path:
    h = tmp_path / name
    h.mkdir()
    (h / "principle_triggers.json").write_text(json.dumps(REGISTRY))
    return h


def _prompt_counter_ok(mod, home: Path):
    # The counter path the hook will actually read must be the file planted above.
    p = Path(mod.PROMPT_COUNTER_PATH)
    if not p.exists():
        p.write_text(json.dumps({"prompts_since_compaction": 50}))


def _util_db(home: Path) -> str:
    import sqlite3
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL)")
    conn.execute("INSERT INTO nodes (id, utility_score) VALUES (?, 0.0)", (PID,))
    conn.commit()
    conn.close()
    return str(db)


class TestUtilityUnknown:
    @pytest.mark.parametrize("data,mode,cls", _case_params())
    def test_u1_direct_call_leaves_file_and_prints_one_line(
            self, data, mode, cls, tmp_path, monkeypatch, capsys):
        home = _util_home(tmp_path)
        mod = _utility(monkeypatch, home)
        _prompt_counter_ok(mod, home)
        state = Path(mod.PRINCIPLE_TRIGGER_STATE_PATH)
        _plant(state, data, mode)
        before = set(os.listdir(home))
        try:
            mod._check_enactments([PID])  # must not raise
        finally:
            _restore_mode(state)
        assert state.read_bytes() == data
        assert not (home / SIDECAR_NAME).exists()
        assert not any(n.endswith(".corrupt") for n in os.listdir(home))
        assert not [n for n in set(os.listdir(home)) - before
                    if n.endswith(".tmp")], os.listdir(home)
        _assert_one_clean_line(capsys.readouterr().err, cls, "enactment count skipped")

    @pytest.mark.parametrize("data,mode,cls", _case_params())
    def test_u1_through_credit_mentions_the_line_survives_the_callers_except(
            self, data, mode, cls, tmp_path, monkeypatch, capsys):
        home = _util_home(tmp_path)
        mod = _utility(monkeypatch, home)
        _prompt_counter_ok(mod, home)
        db = _util_db(home)
        state = Path(mod.PRINCIPLE_TRIGGER_STATE_PATH)
        _plant(state, data, mode)
        try:
            stats = mod.credit_mentions(f"as {PID} says", db_path=db)
        finally:
            _restore_mode(state)
        assert stats["mentioned_count"] == 1  # control: the pipeline really ran
        assert state.read_bytes() == data
        assert not (home / SIDECAR_NAME).exists()
        _assert_one_clean_line(capsys.readouterr().err, cls, "enactment count skipped")

    @pytest.mark.parametrize("data", [CORRUPT, json.dumps(RICH_STATE).encode()],
                             ids=["symlink_to_corrupt", "symlink_to_valid_dict"])
    def test_u1_symlinked_state_is_unknown_link_and_target_untouched(
            self, data, tmp_path, monkeypatch, capsys):
        home = _util_home(tmp_path)
        mod = _utility(monkeypatch, home)
        _prompt_counter_ok(mod, home)
        target = home / "real-state.json"
        target.write_bytes(data)
        link = Path(mod.PRINCIPLE_TRIGGER_STATE_PATH)
        os.symlink(str(target), str(link))
        mod._check_enactments([PID])
        assert os.path.islink(str(link)) and os.readlink(str(link)) == str(target)
        assert target.read_bytes() == data
        assert not any(n.endswith(".corrupt") for n in os.listdir(home))
        err = capsys.readouterr().err
        _assert_one_clean_line(err, "SymlinkStatePath", "enactment count skipped")
        assert "replace the link with a regular file to restore cooldowns" in err

    def test_u1_message_is_bounded_and_one_line(self, tmp_path, monkeypatch, capsys):
        home = _util_home(tmp_path)
        mod = _utility(monkeypatch, home)
        _prompt_counter_ok(mod, home)
        state = Path(mod.PRINCIPLE_TRIGGER_STATE_PATH)
        # bad JSON whose decoder message can carry odd text; keep it simple but
        # make the file long so any unbounded echo would show.
        state.write_bytes(b'{"a": "' + b"x" * 5000)
        mod._check_enactments([PID])
        lines = _marker_lines(capsys.readouterr().err)
        assert len(lines) == 1
        assert len(lines[0]) <= 400, len(lines[0])

    @pytest.mark.parametrize("state_kind", ["absent", "valid_dict"])
    def test_u2_healthy_state_is_byte_identical_to_merge_base(
            self, state_kind, tmp_path, monkeypatch, capsys):
        dev_file = tmp_path / "dev-utility-hook.py"
        dev_file.write_text(_merge_base_text(_UTILITY_REL))
        results = {}
        for label, src in (("new", _UTILITY_PATH), ("dev", dev_file)):
            home = _util_home(tmp_path, label)
            mod = _utility(monkeypatch, home, src)
            _prompt_counter_ok(mod, home)
            state = Path(mod.PRINCIPLE_TRIGGER_STATE_PATH)
            if state_kind == "valid_dict":
                state.write_text(json.dumps(RICH_STATE))
            capsys.readouterr()  # drop any import-time noise
            mod._check_enactments([PID])
            results[label] = (state.read_bytes() if state.exists() else None,
                              capsys.readouterr().err)
        assert results["new"][0] is not None  # control: it really wrote
        want = 1 if state_kind == "absent" else RICH_STATE[PID]["enactments"] + 1
        assert json.loads(results["new"][0])[PID]["enactments"] == want
        assert results["new"] == results["dev"]
        assert not _marker_lines(results["new"][1])


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

def _fresh_core(tmp_path, monkeypatch):
    """A fresh engram_core with DATA_DIR pointed at tmp_path (same recipe as
    tests/test_principle_triggers_registry.py::_fresh_core_with_data_dir)."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    src_engram = str(SRC)
    prior = list(sys.path)
    if src_engram in sys.path:
        sys.path.remove(src_engram)
    sys.path.insert(0, src_engram)
    try:
        import engram_core as core
        assert Path(core.__file__).resolve() == (SRC / "engram_core.py").resolve()
    finally:
        sys.path[:] = prior
        for key in list(sys.modules):
            if key == "tools" or key.startswith("tools."):
                del sys.modules[key]
    monkeypatch.setattr(core, "DATA_DIR", str(tmp_path))
    return core


class TestCoreUnknown:
    @pytest.mark.parametrize("data,mode,cls", _case_params())
    def test_c1_direct_call_leaves_file_and_prints_one_line(
            self, data, mode, cls, tmp_path, monkeypatch, capsys):
        core = _fresh_core(tmp_path, monkeypatch)
        state = tmp_path / STATE_NAME
        _plant(state, data, mode)
        try:
            core._reset_principle_enactments(PID)  # must not raise
        finally:
            _restore_mode(state)
        assert state.read_bytes() == data
        assert not (tmp_path / SIDECAR_NAME).exists()
        assert not any(n.endswith((".corrupt", ".tmp")) for n in os.listdir(tmp_path)), \
            os.listdir(tmp_path)
        _assert_one_clean_line(capsys.readouterr().err, cls, "enactment reset skipped")

    @pytest.mark.parametrize("data", [CORRUPT, json.dumps(RICH_STATE).encode()],
                             ids=["symlink_to_corrupt", "symlink_to_valid_dict"])
    def test_c1_symlinked_state_is_unknown_link_and_target_untouched(
            self, data, tmp_path, monkeypatch, capsys):
        core = _fresh_core(tmp_path, monkeypatch)
        target = tmp_path / "real-state.json"
        target.write_bytes(data)
        link = tmp_path / STATE_NAME
        os.symlink(str(target), str(link))
        core._reset_principle_enactments(PID)
        assert os.path.islink(str(link)) and os.readlink(str(link)) == str(target)
        assert target.read_bytes() == data
        assert not any(n.endswith((".corrupt", ".tmp")) for n in os.listdir(tmp_path))
        err = capsys.readouterr().err
        _assert_one_clean_line(err, "SymlinkStatePath", "enactment reset skipped")
        assert "replace the link with a regular file to restore cooldowns" in err

    @pytest.mark.parametrize("data,cls", [
        pytest.param(CORRUPT, "JSONDecodeError", id="corrupt_json"),
        pytest.param(LIST_BYTES, "NotADict", id="list"),
    ])
    def test_c1_through_a_real_caller_the_line_survives_the_bare_pass(
            self, data, cls, tmp_path):
        # engram_register_exemplar -> engram_epistemic ->
        # `try: core._reset_principle_enactments(...) except Exception: pass`.
        child = textwrap.dedent(r"""
            import json, os, sys
            sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
            data_dir, hexbytes = sys.argv[3], sys.argv[4]
            os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
            import server
            server._configure_paths(data_dir)
            server._ensure_data_dir()
            def ok(r):
                assert "error" not in r, r
                return r
            inc = ok(json.loads(server.engram_add_axiom(payload_json=json.dumps(
                {"claim": "child incident", "basis": "fixture"}))))["axiom_id"]
            lid = ok(json.loads(server.engram_add_lesson(payload_json=json.dumps({
                "claim": "child lesson", "incident_ids": inc,
                "logical_chain": "pattern", "scaffolding_nudge": "watch"}))))["lesson_id"]
            ex = ok(json.loads(server.engram_add_axiom(payload_json=json.dumps(
                {"claim": "child exemplar", "basis": "fixture"}))))["axiom_id"]
            p = os.path.join(data_dir, "principle-trigger-state.json")
            with open(p, "wb") as f:
                f.write(bytes.fromhex(hexbytes))
            r = ok(json.loads(server.engram_register_exemplar(payload_json=json.dumps(
                {"target_id": lid, "exemplar_id": ex}))))
            with open(p, "rb") as f:
                after = f.read().hex()
            print("RESULT:" + json.dumps({"after": after, "names": sorted(os.listdir(data_dir))}))
        """)
        data_dir = tmp_path / "srv"
        data_dir.mkdir()
        proc = subprocess.run(
            [sys.executable, "-c", child, str(SRC), str(PROJECT_ROOT),
             str(data_dir), data.hex()],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "ENGRAM_HOME": str(data_dir)},
        )
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
        assert line, (proc.stdout[-2000:], proc.stderr[-2000:])
        res = json.loads(line[0][len("RESULT:"):])
        assert bytes.fromhex(res["after"]) == data
        assert SIDECAR_NAME not in res["names"]
        _assert_one_clean_line(proc.stderr, cls, "enactment reset skipped")

    @pytest.mark.parametrize("state_kind", ["absent", "valid_dict"])
    def test_c2_healthy_state_is_byte_identical_to_merge_base(
            self, state_kind, tmp_path, monkeypatch, capsys):
        import ast
        import fcntl
        text = _merge_base_text(_CORE_REL)
        tree = ast.parse(text)
        want = {"_with_state_lock", "_reset_principle_enactments"}
        picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
        assert {n.name for n in picked} == want
        dev_home = tmp_path / "dev"
        new_home = tmp_path / "new"
        dev_home.mkdir()
        new_home.mkdir()
        ns = {"os": os, "json": json, "sys": sys, "fcntl": fcntl, "DATA_DIR": str(dev_home)}
        exec(compile(ast.Module(body=picked, type_ignores=[]), "mb_core", "exec"), ns)
        core = _fresh_core(new_home, monkeypatch)
        capsys.readouterr()  # engram_core's import-time stderr is not this contract
        results = {}
        for label, home, fn in (("new", new_home, core._reset_principle_enactments),
                                ("dev", dev_home, ns["_reset_principle_enactments"])):
            if state_kind == "valid_dict":
                (home / STATE_NAME).write_text(json.dumps(RICH_STATE))
            fn(PID)
            results[label] = ((home / STATE_NAME).read_bytes(), capsys.readouterr().err)
        assert json.loads(results["new"][0])[PID]["enactments"] == 0  # control
        assert (state_kind == "absent") or json.loads(results["new"][0])[OTHER] == RICH_STATE[OTHER]
        assert results["new"] == results["dev"]
        assert not _marker_lines(results["new"][1])


# ---------------------------------------------------------------------------
# P1: one fixture table through both classifier copies
# ---------------------------------------------------------------------------

def _mk_absent(d):
    return d / "s.json"


def _mk_dict(d):
    p = d / "s.json"
    p.write_text(json.dumps(RICH_STATE))
    return p


def _mk_bad(d):
    p = d / "s.json"
    p.write_bytes(CORRUPT)
    return p


def _mk_list(d):
    p = d / "s.json"
    p.write_bytes(LIST_BYTES)
    return p


def _mk_mode000(d):
    if _is_root():
        pytest.skip("root bypasses mode 000")
    p = _mk_dict(d)
    p.chmod(0)
    return p


def _mk_fifo(d):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo")
    p = d / "s.json"
    os.mkfifo(str(p))
    return p


def _mk_dir(d):
    p = d / "s.json"
    p.mkdir()
    return p


def _mk_link_valid(d):
    (d / "target.json").write_text(json.dumps(RICH_STATE))
    p = d / "s.json"
    os.symlink(str(d / "target.json"), str(p))
    return p


def _mk_link_corrupt(d):
    (d / "target.json").write_bytes(CORRUPT)
    p = d / "s.json"
    os.symlink(str(d / "target.json"), str(p))
    return p


def _mk_link_dangling(d):
    p = d / "s.json"
    os.symlink(str(d / "nowhere.json"), str(p))
    return p


# id -> (maker, expected (state, reason class, movable))
FIXTURES = {
    "absent": (_mk_absent, ({}, None, False)),
    "dict": (_mk_dict, (RICH_STATE, None, False)),
    "bad_json": (_mk_bad, ({}, "JSONDecodeError", True)),
    "list": (_mk_list, ({}, "NotADict", True)),
    "mode_000": (_mk_mode000, ({}, "PermissionError", False)),
    "fifo": (_mk_fifo, ({}, "OSError", False)),
    "directory": (_mk_dir, ({}, "IsADirectoryError", False)),
    "symlink_to_valid_dict": (_mk_link_valid, ({}, "SymlinkStatePath", False)),
    "symlink_to_corrupt": (_mk_link_corrupt, ({}, "SymlinkStatePath", False)),
    "dangling_symlink": (_mk_link_dangling, ({}, "SymlinkStatePath", False)),
}


def _triple(fn, path):
    state, reason, movable = fn(str(path))
    cls = reason.split(":", 1)[0] if reason else None
    if reason is not None:
        assert ": " in reason, reason
    return state, cls, movable


@pytest.mark.parametrize("name", list(FIXTURES))
def test_p1_both_copies_classify_every_fixture_the_same(name, tmp_path):
    maker, expected = FIXTURES[name]
    p = maker(tmp_path)
    try:
        hook_t = _triple(_hooklib().read_state_json_classified, p)
        core_t = _triple(_core_io().read_state_json_classified, p)
    finally:
        if name == "mode_000":
            _restore_mode(p)
    assert hook_t == core_t, (hook_t, core_t)
    assert hook_t == expected, hook_t


# ---------------------------------------------------------------------------
# S1: the surface hook with a symlinked state path (ruling 5)
# ---------------------------------------------------------------------------

def _surface(monkeypatch, home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return _load(_SURFACE_PATH, "surface_hook_3521r40s1")


def _surface_home(tmp_path: Path) -> Path:
    h = tmp_path / "shome"
    h.mkdir()
    (h / "principle_triggers.json").write_text(json.dumps(REGISTRY))
    return h


@pytest.mark.parametrize("kind,data", [
    pytest.param("corrupt", CORRUPT, id="symlink_to_corrupt"),
    pytest.param("valid", json.dumps(RICH_STATE).encode(), id="symlink_to_valid_dict"),
])
def test_s1_symlinked_state_is_unknown_never_moved_never_written(
        kind, data, tmp_path, monkeypatch):
    home = _surface_home(tmp_path)
    target = home / "real-state.json"
    target.write_bytes(data)
    link = home / STATE_NAME
    os.symlink(str(target), str(link))
    mod = _surface(monkeypatch, home)
    su = []
    text, rendered = mod.check_principle_triggers_full([TRIG], 500, state_unknown=su)
    assert "r40 claim text" in text and len(rendered) == 1  # the nudge still fires
    assert not (home / SIDECAR_NAME).exists()
    assert not os.path.lexists(str(home / SIDECAR_NAME))
    assert os.path.islink(str(link))
    assert os.readlink(str(link)) == str(target)
    assert target.read_bytes() == data  # not moved, not written
    assert len(su) == 1, su
    assert su[0]["reason"] == ("SymlinkStatePath: state path is a symlink; replace the link "
                               "with a regular file to restore cooldowns"), su
    assert su[0]["moved_to"] is None
