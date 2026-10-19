"""#3863 PR 5 (ruling R3): the per-seat ENGRAM home registry.

Contract cells for engram_home_registry (writer + reader), viz discover_agents
and the viz-operator ACL tool.  Every reader refusal falls back (None / glob),
never raises.  The uid rule is exercised through the injected stat_fn."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(REPO_ROOT / "src" / "engram"))  # append, not insert(0)

import engram_home_registry as reg  # noqa: E402
from viz import agents as viz_agents  # noqa: E402


def _acl_module():
    spec = importlib.util.spec_from_file_location(
        "grant_viz_operator_acl_3863", str(REPO_ROOT / "tools" / "grant_viz_operator_acl.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _entry(login, doc):
    p = Path(login) / reg.ENTRY_NAME
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    os.chmod(p, 0o644)
    return p


@pytest.fixture
def login(tmp_path):
    d = tmp_path / "login"
    d.mkdir()
    return d


@pytest.fixture
def home(tmp_path):
    d = tmp_path / "datadir"  # deliberately NOT a .engram* name
    d.mkdir()
    return d


# ---- writer ---------------------------------------------------------------

def test_w1_default_home_writes_no_entry(login):
    (login / ".engram").mkdir()
    assert reg.write_entry(str(login / ".engram"), login_home=str(login)) == "default"
    assert not (login / reg.ENTRY_NAME).exists()


def test_w2_default_home_removes_own_stale_entry(login):
    (login / ".engram").mkdir()
    _entry(login, {"schema": 1, "engram_home": "/somewhere"})
    assert reg.write_entry(str(login / ".engram"), login_home=str(login)) == "removed"
    assert not (login / reg.ENTRY_NAME).exists()


def test_w3_non_default_home_writes_exact_entry_0644(login, home):
    assert reg.write_entry(str(home), login_home=str(login)) == "written"
    p = login / reg.ENTRY_NAME
    assert json.loads(p.read_text()) == {"schema": 1, "engram_home": str(home)}
    assert stat.S_IMODE(p.stat().st_mode) == 0o644
    assert [x.name for x in login.iterdir() if ".tmp." in x.name] == []


def test_w4_write_failure_never_raises(tmp_path, home):
    assert reg.write_entry(str(home), login_home=str(tmp_path / "no-such-login")) == "error"


def test_w5_relative_home_refused(login):
    assert reg.write_entry("rel/dir", login_home=str(login)) in ("error", "written")
    doc = login / reg.ENTRY_NAME
    if doc.exists():
        assert os.path.isabs(json.loads(doc.read_text())["engram_home"])


def test_w6_write_refuses_to_replace_symlink_entry(login, home, tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    os.symlink(target, login / reg.ENTRY_NAME)
    assert reg.write_entry(str(home), login_home=str(login)) == "error"
    assert target.read_text() == "keep"


def test_w7_umask_077_still_gives_a_world_readable_entry(login, home):
    old = os.umask(0o077)
    try:
        assert reg.write_entry(str(home), login_home=str(login)) == "written"
    finally:
        os.umask(old)
    assert stat.S_IMODE((login / reg.ENTRY_NAME).stat().st_mode) == 0o644


def test_w8_stale_temp_files_never_lock_the_writer_out(login, home):
    (login / f"{reg.ENTRY_NAME}.tmp.{os.getpid()}").write_text("stale")
    (login / f"{reg.ENTRY_NAME}.{os.getpid()}.tmp").write_text("stale")
    assert reg.write_entry(str(home), login_home=str(login)) == "written"
    assert reg.read_entry(login) == str(home)


def test_w9_failed_replace_leaves_no_temp_file(login, home, monkeypatch):
    def boom(*a, **k):
        raise OSError("no")
    monkeypatch.setattr(os, "replace", boom)
    assert reg.write_entry(str(home), login_home=str(login)) == "error"
    assert [x.name for x in login.iterdir()] == []


# ---- reader ---------------------------------------------------------------

def test_r1_absent_entry_is_none(login):
    assert reg.read_entry(login) is None
    assert reg.homes_for(login) == []


def test_r2_trusted_entry_returns_home(login, home):
    _entry(login, {"schema": 1, "engram_home": str(home)})
    assert reg.read_entry(login) == str(home)
    assert reg.homes_for(login) == [str(home)]


def test_r3_symlink_entry_refused(login, home, tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"schema": 1, "engram_home": str(home)}))
    os.symlink(real, login / reg.ENTRY_NAME)
    assert reg.read_entry(login) is None


def test_r4_uid_mismatch_refused(login, home):
    _entry(login, {"schema": 1, "engram_home": str(home)})
    real_stat = os.stat

    def other_owner(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p) == str(home):
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)
        return st

    assert reg.read_entry(login, stat_fn=other_owner) is None


@pytest.mark.parametrize("doc", [
    "not json",
    "[]",
    {"schema": 1},
    {"schema": 1, "engram_home": "relative/path"},
    {"schema": 1, "engram_home": "/tmp/a/../b"},
    {"schema": 1, "engram_home": ""},
    {"schema": 1, "engram_home": 7},
])
def test_r5_malformed_or_unsafe_entry_refused(login, doc):
    _entry(login, doc)
    assert reg.read_entry(login) is None


def test_r5b_wrong_schema_refused_even_with_real_home(login, home):
    _entry(login, {"schema": 2, "engram_home": str(home)})
    assert reg.read_entry(login) is None


def test_r5c_dotdot_path_refused_even_when_it_resolves_to_a_real_dir(login, home):
    _entry(login, {"schema": 1, "engram_home": str(home / ".." / home.name)})
    assert (home / ".." / home.name).is_dir()
    assert reg.read_entry(login) is None


def test_r3b_symlink_swapped_in_after_lstat_is_refused(login, home, tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"schema": 1, "engram_home": str(home)}))
    os.symlink(real, login / reg.ENTRY_NAME)
    real_lstat = os.lstat
    # lstat reports the (regular) target: a regular file was there at check time
    lying = lambda p, *a, **k: os.stat(real)  # noqa: E731
    assert reg.read_entry(login, lstat_fn=lying) is None


def test_r7b_oversize_entry_whose_lstat_looked_small_is_refused(login, home):
    p = _entry(login, json.dumps({"schema": 1, "engram_home": str(home), "pad": "x" * 5000}))
    small = types.SimpleNamespace(st_mode=os.lstat(p).st_mode, st_size=10, st_uid=os.getuid())
    assert reg.read_entry(login, lstat_fn=lambda *_a, **_k: small) is None


@pytest.mark.parametrize("schema", [True, 1.0, "1", None])
def test_r5d_schema_must_be_the_int_one(login, home, schema):
    _entry(login, {"schema": schema, "engram_home": str(home)})
    assert reg.read_entry(login) is None


def test_r9_hard_linked_entry_refused(login, home):
    p = _entry(login, {"schema": 1, "engram_home": str(home)})
    os.link(p, login / "other-name")  # st_nlink 2: another name for the same file
    assert reg.read_entry(login) is None


def test_r10_entry_owner_must_own_the_login_home(login, home):
    _entry(login, {"schema": 1, "engram_home": str(home)})
    real_stat = os.stat

    def other_login_owner(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p) == str(login):
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)
        return st

    assert reg.read_entry(login, stat_fn=other_login_owner) is None


def test_r11_file_swapped_after_lstat_is_refused(login, home, tmp_path):
    p = _entry(login, {"schema": 1, "engram_home": str(home)})
    decoy = _entry(tmp_path, {"schema": 1, "engram_home": str(home)})
    # lstat describes a DIFFERENT regular file than the one that gets opened
    assert reg.read_entry(login, lstat_fn=lambda *_a, **_k: os.lstat(decoy)) is None


def test_r12_owner_is_read_from_the_opened_descriptor_not_the_earlier_lstat(login, home):
    p = _entry(login, {"schema": 1, "engram_home": str(home)})
    real = os.lstat(p)
    stale = types.SimpleNamespace(
        st_mode=real.st_mode, st_size=real.st_size, st_ino=real.st_ino,
        st_dev=real.st_dev, st_nlink=1, st_uid=real.st_uid + 1)
    assert reg.read_entry(login, lstat_fn=lambda *_a, **_k: stale) == str(home)


def test_r6_named_path_not_a_directory_refused(login, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    _entry(login, {"schema": 1, "engram_home": str(f)})
    assert reg.read_entry(login) is None


def test_r7_oversize_entry_refused(login, home):
    _entry(login, json.dumps({"schema": 1, "engram_home": str(home), "pad": "x" * 5000}))
    assert reg.read_entry(login) is None


def test_r8_fifo_entry_refused_without_hanging(login):
    os.mkfifo(login / reg.ENTRY_NAME)
    assert reg.read_entry(login) is None


# ---- viz discovery ---------------------------------------------------------

def _seat(home, name):
    (home).mkdir(parents=True, exist_ok=True)
    (home / "knowledge.db").write_text("")
    (home / "config.json").write_text(json.dumps({"agent_name": name}))


def test_v1_viz_finds_agent_whose_home_is_not_a_dot_engram_dir(login, home):
    _seat(home, "oddseat")
    _entry(login, {"schema": 1, "engram_home": str(home)})
    found = viz_agents.discover_agents(scan_roots=[str(login)])
    assert found["oddseat"]["db"] == str(home / "knowledge.db")


def test_v2_viz_glob_still_finds_default_home_without_entry(login):
    _seat(login / ".engram", "plainseat")
    found = viz_agents.discover_agents(scan_roots=[str(login)])
    assert list(found) == ["plainseat"]


def test_v3_registry_does_not_replace_glob(login, home):
    _seat(home, "oddseat")
    _seat(login / ".engram", "plainseat")
    _entry(login, {"schema": 1, "engram_home": str(home)})
    found = viz_agents.discover_agents(scan_roots=[str(login)])
    assert sorted(found) == ["oddseat", "plainseat"]


def test_v4_home_found_both_ways_counts_once(login):
    _seat(login / ".engram2", "dup")
    _entry(login, {"schema": 1, "engram_home": str(login / ".engram2")})
    found = viz_agents.discover_agents(scan_roots=[str(login)])
    assert list(found) == ["dup"]


@pytest.mark.parametrize("name", ["active-baseline", "current.bak", "baseline"])
def test_v7_registered_home_is_not_dropped_by_the_backup_name_filter(tmp_path, login, name):
    home = tmp_path / name
    _seat(home, "oddseat")
    _entry(login, {"schema": 1, "engram_home": str(home)})
    found = viz_agents.discover_agents(scan_roots=[str(login)])
    assert found["oddseat"]["db"] == str(home / "knowledge.db")


@pytest.mark.parametrize("name", ["active-baseline", "current.bak"])
def test_v8_registered_backup_named_home_beats_a_same_name_default(tmp_path, login, name):
    home = tmp_path / name
    _seat(home, "same")
    _seat(login / ".engram", "same")
    _entry(login, {"schema": 1, "engram_home": str(home)})
    found = viz_agents.discover_agents(scan_roots=[str(login)])
    assert found["same"]["db"] == str(home / "knowledge.db")


def test_v9_glob_candidate_with_backup_name_is_still_skipped(login):
    _seat(login / ".engram-baseline", "bk")
    _seat(login / ".engram.bak", "bk2")
    assert viz_agents.discover_agents(scan_roots=[str(login)]) == {}


def test_v5_untrusted_entry_falls_back_to_glob(login, home):
    _seat(home, "oddseat")
    _seat(login / ".engram", "plainseat")
    _entry(login, {"schema": 1, "engram_home": "relative"})
    assert list(viz_agents.discover_agents(scan_roots=[str(login)])) == ["plainseat"]


def test_v6_registry_first_wins_a_name_collision(login, home):
    _seat(home, "same")
    _seat(login / ".engram", "same")
    _entry(login, {"schema": 1, "engram_home": str(home)})
    found = viz_agents.discover_agents(scan_roots=[str(login)])
    assert found["same"]["db"] == str(home / "knowledge.db")


# ---- ACL tool ---------------------------------------------------------------

def test_a1_acl_targets_registered_home(login, home):
    (home / "health-snapshots").mkdir()
    _entry(login, {"schema": 1, "engram_home": str(home)})
    plan = _acl_module().plan_for_seat(str(login), "viz")
    assert plan.actionable
    assert plan.snapshots_dir == str(home / "health-snapshots")


def test_a2_acl_default_without_entry(login):
    (login / ".engram" / "health-snapshots").mkdir(parents=True)
    plan = _acl_module().plan_for_seat(str(login), "viz")
    assert plan.snapshots_dir == str(login / ".engram" / "health-snapshots")


def test_a3_acl_untrusted_entry_falls_back(login, home):
    (home / "health-snapshots").mkdir()
    (login / ".engram" / "health-snapshots").mkdir(parents=True)
    _entry(login, {"schema": 1, "engram_home": "../escape"})
    plan = _acl_module().plan_for_seat(str(login), "viz")
    assert plan.snapshots_dir == str(login / ".engram" / "health-snapshots")


@pytest.mark.parametrize("where", ["default", "registered"])
def test_a5_symlinked_health_snapshots_is_refused_even_to_a_dir_the_seat_owns(tmp_path, login, home, where):
    target = tmp_path / "other-tree"
    target.mkdir()
    base = login / ".engram" if where == "default" else home
    base.mkdir(parents=True, exist_ok=True)
    os.symlink(target, base / "health-snapshots")
    if where == "registered":
        _entry(login, {"schema": 1, "engram_home": str(home)})
    plan = _acl_module().plan_for_seat(str(login), "viz")
    assert not plan.actionable and plan.commands == []
    assert "symlink" in plan.skip_reason and str(base / "health-snapshots") in plan.skip_reason


@pytest.mark.skipif(os.getuid() == 0, reason="needs a root-owned target that is foreign to the caller")
@pytest.mark.parametrize("where", ["default", "registered"])
def test_a5b_symlink_to_a_real_root_owned_dir_is_refused(login, home, where):
    base = login / ".engram" if where == "default" else home
    base.mkdir(parents=True, exist_ok=True)
    os.symlink("/etc", base / "health-snapshots")  # root-owned on macOS and Linux
    assert os.stat("/etc").st_uid != os.getuid()
    if where == "registered":
        _entry(login, {"schema": 1, "engram_home": str(home)})
    plan = _acl_module().plan_for_seat(str(login), "viz")
    assert not plan.actionable and plan.commands == [] and "symlink" in plan.skip_reason


@pytest.mark.parametrize("where", ["default", "registered"])
def test_a6_real_seat_owned_health_snapshots_is_still_granted(login, home, where):
    base = login / ".engram" if where == "default" else home
    (base / "health-snapshots").mkdir(parents=True)
    if where == "registered":
        _entry(login, {"schema": 1, "engram_home": str(home)})
    plan = _acl_module().plan_for_seat(str(login), "viz")
    snaps = str(base / "health-snapshots")
    # byte-identical to the pre-guard plan for a seat-owned real directory
    assert plan.actionable and plan.skip_reason is None and plan.snapshots_dir == snaps
    assert plan.commands == [["setfacl", "-R", "-m",
                              "u:viz:rX,m::rX,d:u:viz:rX,d:m::rX", snaps]]


@pytest.mark.parametrize("where", ["default", "registered"])
def test_a7_health_snapshots_owned_by_another_user_is_refused(login, home, monkeypatch, where):
    base = login / ".engram" if where == "default" else home
    snaps = base / "health-snapshots"
    snaps.mkdir(parents=True)
    if where == "registered":
        _entry(login, {"schema": 1, "engram_home": str(home)})
    real_stat = os.stat

    def foreign(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p) == str(snaps):
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)
        return st

    mod = _acl_module()
    monkeypatch.setattr(mod.os, "stat", foreign)
    plan = mod.plan_for_seat(str(login), "viz")
    assert not plan.actionable and plan.commands == []
    me = os.getuid()
    assert f"uid {me + 1}" in plan.skip_reason and f"uid {me}" in plan.skip_reason


def test_a8_owner_is_compared_to_the_seat_not_to_the_privileged_caller(login, monkeypatch):
    (login / ".engram" / "health-snapshots").mkdir(parents=True)
    mod = _acl_module()
    monkeypatch.setattr(mod.os, "getuid", lambda: 0)  # the tool runs as root, the seat does not
    plan = mod.plan_for_seat(str(login), "viz")
    assert plan.actionable, plan.skip_reason


def test_a9_unreadable_health_snapshots_is_refused_not_granted(login, monkeypatch):
    snaps = login / ".engram" / "health-snapshots"
    snaps.mkdir(parents=True)
    mod = _acl_module()
    real_lstat = os.lstat

    def boom(p, *a, **k):
        if str(p) == str(snaps):
            raise PermissionError("no")
        return real_lstat(p, *a, **k)

    monkeypatch.setattr(mod.os, "lstat", boom)
    plan = mod.plan_for_seat(str(login), "viz")
    assert not plan.actionable and plan.commands == []
    assert "cannot inspect" in plan.skip_reason


def test_a4_acl_falls_back_to_default_when_registered_home_has_no_snapshots(login, home):
    (login / ".engram" / "health-snapshots").mkdir(parents=True)
    _entry(login, {"schema": 1, "engram_home": str(home)})  # home has no health-snapshots
    plan = _acl_module().plan_for_seat(str(login), "viz")
    assert plan.actionable
    assert plan.snapshots_dir == str(login / ".engram" / "health-snapshots")


# ---- SessionStart hook ------------------------------------------------------

_HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook(monkeypatch, engram_home, login):
    from unittest.mock import MagicMock
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    monkeypatch.setenv("HOME", str(login))
    stub = MagicMock()
    stub.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub)
    sys.modules.pop("engram_session_start_hook_3863", None)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook_3863", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_h1_hook_records_non_default_home_and_prints_nothing(login, home, monkeypatch, capsys):
    mod = _load_hook(monkeypatch, home, login)
    capsys.readouterr()
    mod._record_engram_home()
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert reg.read_entry(login) == str(home)


def test_h2_hook_default_home_writes_no_entry(login, monkeypatch):
    (login / ".engram").mkdir()
    mod = _load_hook(monkeypatch, login / ".engram", login)
    mod._record_engram_home()
    assert not (login / reg.ENTRY_NAME).exists()


def test_h3_hook_registry_failure_never_raises(login, home, monkeypatch, capsys):
    mod = _load_hook(monkeypatch, home, login)
    monkeypatch.setattr(mod, "resolve_runtime_dir", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    capsys.readouterr()
    mod._record_engram_home()
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_h4_hook_main_impl_calls_the_recorder():
    import ast
    tree = ast.parse(_HOOK_PATH.read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_main_impl")
    calls = [c.func.id for c in ast.walk(main) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert "_record_engram_home" in calls
