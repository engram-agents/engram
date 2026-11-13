"""Contract-first tests for #1989(a): a guarded config.json writer and a loud
failure at the turn advance when config.json is absent.

FRAMING: this is a bug-guard, not a security boundary.  It stops ENGRAM's own
writer shrinking config.json; it stops nobody who can write the file.  It does
not catch a valid-but-wrong config (right keys, wrong values).

Every cell runs on a tmp ENGRAM home (tmp_path) -- never the real ~/.engram.

Contract
  A1  a non-dict payload raises ConfigShapeError, writes nothing.
  A2  top-level key loss raises ConfigShrinkError (names + path, capped, no values),
      writes nothing, creates NO backup; allow_key_loss=True writes and logs.
  A3  changed bytes -> stamped backup of the old bytes first; unchanged -> no backup,
      no write; a failing backup aborts the write.
  A4  newest SEVEN backups kept, sorted by parsed stamp + numeric suffix; the prune
      is loud-but-not-fatal and only unlinks regular files / symlinks.
  B   _set_current_turn raises ConfigMissingError on an absent file, creates nothing.
  C   the turn-advance tool returns an error result (str(exc)); process stays alive.
  D   compact record writers are unaffected.
  G   the backup name is gitignored (both shapes) and seen by the tracked-backup checker.
"""

import importlib
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PREFIX = "config.json.engram-backup."


def _fresh_server(tmp_dir):
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(tmp_dir)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


@pytest.fixture
def cio():
    return importlib.import_module("engram_config_io")


@pytest.fixture
def fg(cio):
    return importlib.import_module("engram_file_guard")


def _backups(d):
    return sorted(p.name for p in Path(d).iterdir() if p.name.startswith(PREFIX))


def _pin_clock(fg, monkeypatch, stamps):
    """CLOCK yields the given datetimes in order, then repeats the last."""
    it = iter(stamps)
    last = [stamps[0]]

    def clock():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    monkeypatch.setattr(fg, "CLOCK", clock)


def _cfg(tmp_path, data):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data, indent=2) + "\n")
    return p


# ------------------------------------------------------------------- A1 ---

@pytest.mark.parametrize("bad", [[1, 2], "x", None, 3])
def test_a1_non_dict_raises_shape_error_and_writes_nothing(tmp_path, cio, bad):
    p = _cfg(tmp_path, {"a": 1})
    before = p.read_bytes()
    with pytest.raises(cio.ConfigShapeError):
        cio.write_engram_config(p, bad)
    assert issubclass(cio.ConfigShapeError, ValueError)
    assert p.read_bytes() == before
    assert [x.name for x in tmp_path.iterdir()] == ["config.json"]


def test_a1_non_dict_on_absent_file_creates_nothing(tmp_path, cio):
    with pytest.raises(cio.ConfigShapeError):
        cio.write_engram_config(tmp_path / "config.json", ["x"])
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------- A2 ---

def test_a2_shrink_refused_names_keys_and_path_not_values(tmp_path, cio):
    p = _cfg(tmp_path, {"mode": "SECRETVALUE-1", "agent_name": "x", "keep": 1})
    before = p.read_bytes()
    with pytest.raises(cio.ConfigShrinkError) as ei:
        cio.write_engram_config(p, {"keep": 2})
    msg = str(ei.value)
    assert "agent_name" in msg and "mode" in msg and str(p) in msg
    assert "SECRETVALUE" not in msg and "{" not in msg
    assert p.read_bytes() == before


def test_a2_refused_shrink_creates_no_backup(tmp_path, cio):
    p = _cfg(tmp_path, {"a": 1, "b": 2})
    with pytest.raises(cio.ConfigShrinkError):
        cio.write_engram_config(p, {"a": 1})
    assert _backups(tmp_path) == []
    assert [x.name for x in tmp_path.iterdir()] == ["config.json"]


def test_a2_message_caps_at_20_sorted_names_and_gives_total(tmp_path, cio):
    p = _cfg(tmp_path, {**{f"k{i:02d}": i for i in range(30)}, "keep": 1})
    with pytest.raises(cio.ConfigShrinkError) as ei:
        cio.write_engram_config(p, {"keep": 1})
    msg = str(ei.value)
    assert "k00" in msg and "k19" in msg
    assert "k20" not in msg and "k29" not in msg
    assert "30" in msg


def test_a2_unreadable_existing_file_propagates_and_is_untouched(tmp_path, cio, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    p = tmp_path / "config.json"
    p.write_text('{"a": ')
    with pytest.raises(cio.ConfigUnreadableError):
        cio.write_engram_config(p, {"a": 1})
    assert p.read_text() == '{"a": '
    assert _backups(tmp_path) == []


def test_a2_allow_key_loss_writes_and_logs_the_lost_names(tmp_path, cio, capsys):
    p = _cfg(tmp_path, {"a": 1, "gone_key": "VALUE-ZZ"})
    cio.write_engram_config(p, {"a": 1}, allow_key_loss=True)
    assert json.loads(p.read_text()) == {"a": 1}
    err = capsys.readouterr().err
    assert "gone_key" in err and "VALUE-ZZ" not in err
    assert len(_backups(tmp_path)) == 1


def test_a2_added_and_nested_changes_are_not_loss(tmp_path, cio):
    p = _cfg(tmp_path, {"memory": {"current_turn": 1, "x": 2}, "a": 1})
    cio.write_engram_config(p, {"memory": {"current_turn": 2}, "a": 1, "new": 3})
    assert json.loads(p.read_text())["memory"] == {"current_turn": 2}


# ------------------------------------------------------------------- A3 ---

def test_a3_changed_bytes_backed_up_first_with_old_bytes(tmp_path, cio):
    p = _cfg(tmp_path, {"a": 1})
    old = p.read_bytes()
    bk = cio.write_engram_config(p, {"a": 2})
    assert bk is not None and bk.parent == tmp_path
    assert bk.name.startswith(PREFIX)
    assert bk.read_bytes() == old
    assert json.loads(p.read_text()) == {"a": 2}


def test_a3_unchanged_bytes_no_backup_no_write_returns_none(tmp_path, cio, monkeypatch):
    p = _cfg(tmp_path, {"a": 1})
    calls = []
    monkeypatch.setattr(cio, "write_config_json", lambda *a, **k: calls.append(a))
    assert cio.write_engram_config(p, {"a": 1}) is None
    assert calls == [] and _backups(tmp_path) == []


def test_a3_absent_file_is_created_without_backup(tmp_path, cio):
    p = tmp_path / "config.json"
    assert cio.write_engram_config(p, {"a": 1}) is None
    assert json.loads(p.read_text()) == {"a": 1}
    assert _backups(tmp_path) == []


def test_a3_backup_failure_aborts_the_write(tmp_path, cio, fg, monkeypatch):
    p = _cfg(tmp_path, {"a": 1})
    before = p.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(fg, "copy_exclusive", boom)
    with pytest.raises(OSError):
        cio.write_engram_config(p, {"a": 2})
    assert p.read_bytes() == before


def test_a3_backup_keeps_source_mode(tmp_path, cio):
    p = _cfg(tmp_path, {"a": 1})
    os.chmod(p, 0o640)
    bk = cio.write_engram_config(p, {"a": 2})
    assert stat.S_IMODE(os.stat(bk).st_mode) == 0o640


def test_a5_write_goes_through_write_config_json(tmp_path, cio, monkeypatch):
    p = _cfg(tmp_path, {"a": 1})
    seen = []
    real = cio.write_config_json
    monkeypatch.setattr(cio, "write_config_json", lambda path, data: (seen.append(data), real(path, data)))
    cio.write_engram_config(p, {"a": 2})
    assert seen == [{"a": 2}]


# ------------------------------------------------------------------- A4 ---

def _times(n):
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [base + timedelta(seconds=i) for i in range(n)]


def test_a4_keeps_newest_seven_including_the_new_one(tmp_path, cio, fg, monkeypatch):
    ts = _times(12)
    _pin_clock(fg, monkeypatch, ts)
    p = _cfg(tmp_path, {"v": 0})
    for i in range(1, 11):
        cio.write_engram_config(p, {"v": i})
    got = _backups(tmp_path)
    want = sorted(PREFIX + t.strftime("%Y%m%dT%H%M%SZ") for t in ts[3:10])
    assert got == want


def test_a4_same_second_collisions_sort_by_numeric_suffix(tmp_path, cio, fg, monkeypatch):
    _pin_clock(fg, monkeypatch, _times(1))  # one stamp, forever
    stamp = "20260101T000000Z"
    p = _cfg(tmp_path, {"v": 0})
    for i in range(1, 13):  # 12 writes -> 12 backups: stamp, -1 .. -11
        cio.write_engram_config(p, {"v": i})
    want = sorted(PREFIX + stamp + f"-{n}" for n in range(5, 12))
    # newest seven are -5 .. -11 (name order would drop -10 and keep -2..-4)
    assert _backups(tmp_path) == want
    assert json.loads((tmp_path / (PREFIX + stamp + "-11")).read_text()) == {"v": 11}


def _make_old(tmp_path, cio, fg, monkeypatch, count):
    _pin_clock(fg, monkeypatch, _times(count + 2))
    p = _cfg(tmp_path, {"v": 0})
    for i in range(1, count + 1):
        cio.write_engram_config(p, {"v": i})
    return p


def test_a4_directory_at_matching_name_is_skipped_loudly(tmp_path, cio, fg, monkeypatch, capsys):
    d = tmp_path / (PREFIX + "20250101T000000Z")
    d.mkdir()
    (d / "inner").write_text("keep")
    p = _make_old(tmp_path, cio, fg, monkeypatch, 9)
    assert (d / "inner").read_text() == "keep"
    assert "20250101T000000Z" in capsys.readouterr().err
    assert json.loads(p.read_text()) == {"v": 9}


def test_a4_partial_temp_and_unrelated_names_are_never_deleted(tmp_path, cio, fg, monkeypatch):
    a = tmp_path / (PREFIX + "20250101T000000Z.partial-abc123")
    b = tmp_path / "config.json.bak.20250101T000000Z"
    c = tmp_path / (PREFIX + "garbage")
    for f in (a, b, c):
        f.write_text("x")
    _make_old(tmp_path, cio, fg, monkeypatch, 10)
    assert a.exists() and b.exists() and c.exists()


def test_a4_symlink_is_unlinked_not_followed(tmp_path, cio, fg, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    link = tmp_path / (PREFIX + "20250101T000000Z")
    link.symlink_to(outside)
    _make_old(tmp_path, cio, fg, monkeypatch, 9)
    assert not os.path.lexists(link)
    assert outside.read_text() == "precious"


def test_a4_failed_listing_is_reported_and_not_fatal(tmp_path, cio, fg, monkeypatch, capsys):
    p = _cfg(tmp_path, {"v": 0})
    real = os.scandir

    def bad(path=".", *a, **k):
        if isinstance(path, (str, os.PathLike)) and Path(path) == tmp_path:
            raise PermissionError("nope")
        return real(path, *a, **k)
    monkeypatch.setattr(os, "scandir", bad)
    cio.write_engram_config(p, {"v": 1})
    assert json.loads(p.read_text()) == {"v": 1}
    assert capsys.readouterr().err.strip() != ""


def test_a4_failed_unlink_is_reported_and_not_fatal(tmp_path, cio, fg, monkeypatch, capsys):
    p = _make_old(tmp_path, cio, fg, monkeypatch, 7)
    capsys.readouterr()
    real = os.unlink

    def bad(path, *a, **k):
        if Path(path).name.startswith(PREFIX) and ".partial-" not in str(path):
            raise PermissionError("nope")
        return real(path, *a, **k)
    monkeypatch.setattr(os, "unlink", bad)
    cio.write_engram_config(p, {"v": 99})
    assert json.loads(p.read_text()) == {"v": 99}
    assert capsys.readouterr().err.strip() != ""


def test_a4_a_vanished_file_during_prune_is_tolerated(tmp_path, cio, fg, monkeypatch):
    p = _make_old(tmp_path, cio, fg, monkeypatch, 7)
    real = os.unlink

    def gone(path, *a, **k):
        real(path, *a, **k)
        raise FileNotFoundError(path)
    monkeypatch.setattr(os, "unlink", gone)
    cio.write_engram_config(p, {"v": 99})
    assert json.loads(p.read_text()) == {"v": 99}


# -------------------------------------------------------------------- B ---

def test_b1_set_current_turn_absent_raises_names_path_backups_and_recovery(tmp_path):
    home = tmp_path / "home with space"
    home.mkdir()
    _fresh_server(str(home))
    import engram_core as core
    import engram_config_io as cio
    bk = core.CONFIG_PATH.parent / (PREFIX + "20260101T000000Z")
    bk.write_text("{}")
    core.CONFIG_PATH.unlink()
    with pytest.raises(cio.ConfigMissingError) as ei:
        core._set_current_turn(5)
    msg = str(ei.value)
    assert shlex.quote(str(core.CONFIG_PATH)) in msg
    assert shlex.quote(str(bk)) in msg
    assert "cp " in msg
    assert not core.CONFIG_PATH.exists()


def test_b1_missing_error_with_no_backup_still_names_path(tmp_path):
    _fresh_server(str(tmp_path))
    import engram_core as core
    import engram_config_io as cio
    core.CONFIG_PATH.unlink()
    with pytest.raises(cio.ConfigMissingError) as ei:
        core._set_current_turn(5)
    assert str(core.CONFIG_PATH) in str(ei.value)
    assert not core.CONFIG_PATH.exists()


def test_b2_present_config_advances_and_keeps_every_key(tmp_path):
    _fresh_server(str(tmp_path))
    import engram_core as core
    core.CONFIG_PATH.write_text(json.dumps({"memory": {"current_turn": 30}, "mode": "m", "x": [1]}))
    core._set_current_turn(31)
    got = json.loads(core.CONFIG_PATH.read_text())
    assert got == {"memory": {"current_turn": 31}, "mode": "m", "x": [1]}
    assert len(_backups(core.CONFIG_PATH.parent)) == 1


# -------------------------------------------------------------------- C ---

def test_c1_advance_turn_tool_returns_error_with_path_and_creates_nothing(tmp_path):
    server = _fresh_server(str(tmp_path))
    import engram_core as core
    server.engram_diagnose()  # run the one-time schema bootstrap (it opens the DB, which re-creates an absent default config)
    core.CONFIG_PATH.write_text(json.dumps({"memory": {"current_turn": 30}, "mode": "m"}))
    saved = core.CONFIG_PATH.read_bytes()
    core.CONFIG_PATH.unlink()
    out = json.loads(server.engram_advance_turn(json.dumps({"message": "sleep"})))
    assert "error" in out
    assert shlex.quote(str(core.CONFIG_PATH)) in out["error"]
    assert "ConfigMissingError(" not in out["error"]  # str(exc), not a repr wrapper
    assert not core.CONFIG_PATH.exists()
    core.CONFIG_PATH.write_bytes(saved)  # the counter was not reset
    assert core._get_current_turn() == 30


def test_c2_the_process_is_alive_after_the_error(tmp_path):
    server = _fresh_server(str(tmp_path))
    import engram_core as core
    server.engram_diagnose()
    core.CONFIG_PATH.unlink()
    out = json.loads(server.engram_advance_turn(json.dumps({"message": "sleep"})))
    assert "error" in out
    later = json.loads(server.engram_diagnose())
    assert "error" not in later


# -------------------------------------------------------------------- D ---

def test_d_compact_record_write_makes_no_backup(tmp_path):
    _fresh_server(str(tmp_path))
    import engram_core as core
    import engram_lifecycle as lc
    lc._write_last_checkpoint_record("t1", "s", {})
    lc._write_last_checkpoint_record("t2", "s", {})
    compact = core.DATA_DIR / "compact"
    assert [x.name for x in compact.iterdir()] == ["last-checkpoint.json"]
    assert _backups(core.DATA_DIR) == []


# -------------------------------------------------------------------- G ---

def _git(home, *args):
    return subprocess.run(["git", "-C", str(home), *args], capture_output=True, text=True)


def test_g_backup_and_partial_names_are_gitignored(tmp_path):
    _fresh_server(str(tmp_path / "x"))
    import engram_core as core
    assert "config.json.engram-backup.*" in core.ENGRAM_GITIGNORE_PATTERNS
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("\n".join(core.ENGRAM_GITIGNORE_PATTERNS) + "\n")
    for name in (PREFIX + "20260101T000000Z", PREFIX + "20260101T000000Z-3",
                 PREFIX + "20260101T000000Z.partial-ab12cd"):
        assert _git(repo, "check-ignore", "-q", name).returncode == 0, name
    assert _git(repo, "check-ignore", "-q", "config.json").returncode == 1


def test_g_tracked_backup_checker_sees_the_backup_name(tmp_path):
    _fresh_server(str(tmp_path / "x"))
    import engram_core as core
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    for n in ("config.json", PREFIX + "20260101T000000Z", PREFIX + "20260101T000000Z-2"):
        (repo / n).write_text("{}")
    assert _git(repo, "add", "-f", ".").returncode == 0
    assert sorted(core._tracked_binary_paths(repo)) == sorted(
        [PREFIX + "20260101T000000Z", PREFIX + "20260101T000000Z-2"])


# ------------------------------------------------------------------ pin ---

def test_pin_4705_db_open_regenerates_default_config_today(tmp_path):
    """Pins TODAY's behaviour; engram#4705 removes it. The #4705 fix turns this
    cell red on purpose and replaces it.

    With a database present and config.json absent, one ``_get_db()`` open
    re-creates config.json as the default config (identity keys blank)."""
    server = _fresh_server(str(tmp_path))
    import engram_core as core
    server.engram_diagnose()  # one-time schema bootstrap creates the database
    assert core.DB_PATH.exists()
    core.CONFIG_PATH.unlink()
    assert not core.CONFIG_PATH.exists()
    core._get_db().close()
    assert core.CONFIG_PATH.exists()
    assert json.loads(core.CONFIG_PATH.read_text())["self_lineage"] == ""
