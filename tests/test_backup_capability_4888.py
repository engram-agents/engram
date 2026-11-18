"""Contract tests for #4888 SQL-backup capability on no-iterdump backends."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "engram"
import engram_backup  # noqa: E402
from viz.data import health as health_data  # noqa: E402


class _Proxy:
    """Use a Connection subclass while keeping sqlite3's C backup target real."""

    def __init__(self, factory):
        self._factory = factory

    def connect(self, *args, **kwargs):
        kwargs.setdefault("factory", self._factory)
        return sqlite3.connect(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(sqlite3, name)


class _WithoutIterdump(sqlite3.Connection):
    """A real connection whose iterdump attribute is genuinely absent."""

    def __getattribute__(self, name):
        if name == "iterdump":
            raise AttributeError(name)
        return super().__getattribute__(name)


class _InvalidNativeIterdump(sqlite3.Connection):
    def iterdump(self):
        # Parses, but is not a dump_stripped output: it cannot satisfy the
        # final COMMIT/user_version restore contract.
        return iter(("BEGIN TRANSACTION;", "CREATE TABLE nodes(id TEXT);", "COMMIT;"))


def _source_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY, claim TEXT, embedding BLOB)")
    con.execute("INSERT INTO nodes VALUES (?, ?, ?)", ("ob_probe", "known value", b"embedding"))
    con.execute("PRAGMA user_version = 4888")
    con.commit()
    con.close()


def _assert_restores(path: Path) -> None:
    con = sqlite3.connect(":memory:")
    con.executescript(path.read_text(encoding="utf-8"))
    assert con.execute("SELECT id, claim, embedding FROM nodes").fetchone() == (
        "ob_probe", "known value", None,
    )
    assert con.execute("PRAGMA user_version").fetchone()[0] == 4888
    con.close()


def test_native_iterator_control_roundtrips(tmp_path):
    src, out = tmp_path / "source.db", tmp_path / "backup.sql"
    _source_db(src)
    stats = engram_backup.dump_stripped(str(src), str(out))
    assert stats["iterator_kind"] == "native"
    _assert_restores(out)


def test_real_connection_without_iterdump_uses_stdlib_helper_and_roundtrips(tmp_path, monkeypatch):
    src, out = tmp_path / "source.db", tmp_path / "backup.sql"
    _source_db(src)
    monkeypatch.setattr(engram_backup, "sqlite3", _Proxy(_WithoutIterdump))

    stats = engram_backup.dump_stripped(str(src), str(out))

    assert stats["iterator_kind"] == "stdlib-helper"
    _assert_restores(out)


def test_unavailable_helper_refuses_before_sql_temp_and_preserves_old_target(tmp_path, monkeypatch):
    src, out = tmp_path / "source.db", tmp_path / "backup.sql"
    _source_db(src)
    out.write_bytes(b"old verified backup\n")
    before = out.read_bytes()
    listing = sorted(p.name for p in tmp_path.iterdir())
    monkeypatch.setattr(engram_backup, "sqlite3", _Proxy(_WithoutIterdump))

    def unavailable():
        raise engram_backup.BackupCapabilityError("SQL backup capability unavailable: synthetic missing helper")

    monkeypatch.setattr(engram_backup, "_load_stdlib_iterdump", unavailable)
    with pytest.raises(engram_backup.BackupCapabilityError, match="SQL backup capability unavailable"):
        engram_backup.dump_stripped(str(src), str(out))

    assert out.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == listing


def test_stdlib_loader_refuses_a_helper_without_callable_iterdump(monkeypatch):
    class Loader:
        def exec_module(self, module):
            pass

    monkeypatch.setattr(
        engram_backup.importlib.util, "spec_from_file_location", lambda *args: types.SimpleNamespace(loader=Loader())
    )
    monkeypatch.setattr(engram_backup.importlib.util, "module_from_spec", lambda spec: types.SimpleNamespace())
    with pytest.raises(engram_backup.BackupCapabilityError, match="does not provide callable _iterdump"):
        engram_backup._load_stdlib_iterdump()


def test_invalid_native_iterator_is_not_masked_by_helper_retry(tmp_path, monkeypatch):
    src, out = tmp_path / "source.db", tmp_path / "backup.sql"
    _source_db(src)
    out.write_bytes(b"old verified backup\n")
    before = out.read_bytes()
    monkeypatch.setattr(engram_backup, "sqlite3", _Proxy(_InvalidNativeIterdump))

    def helper_must_not_run():
        raise AssertionError("native failure must not retry through the helper")

    monkeypatch.setattr(engram_backup, "_load_stdlib_iterdump", helper_must_not_run)
    with pytest.raises(RuntimeError, match="dump verification failed"):
        engram_backup.dump_stripped(str(src), str(out))
    assert out.read_bytes() == before


def test_alias_before_import_uses_exact_helper_without_rewriting_alias(tmp_path):
    """An alias installed before import must not make us depend on sqlite3.dump."""
    code = r'''
import pathlib, sqlite3 as host, sys, types
class NoIter(host.Connection):
    def __getattribute__(self, name):
        if name == "iterdump": raise AttributeError(name)
        return super().__getattribute__(name)
alias = types.ModuleType("sqlite3")
for name in dir(host): setattr(alias, name, getattr(host, name))
def connect(*args, **kwargs):
    kwargs.setdefault("factory", NoIter)
    return host.connect(*args, **kwargs)
alias.connect = connect
sys.modules["sqlite3"] = alias
sys.path.insert(0, sys.argv[1])
import engram_backup
assert sys.modules["sqlite3"] is alias
src, out = sys.argv[2:]
con = host.connect(src)
con.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY, claim TEXT, embedding BLOB)")
con.execute("INSERT INTO nodes VALUES ('ob_1', 'alias value', X'01')")
con.execute("PRAGMA user_version = 4888")
con.commit(); con.close()
stats = engram_backup.dump_stripped(src, out)
assert stats["iterator_kind"] == "stdlib-helper"
assert sys.modules["sqlite3"] is alias
con = host.connect(":memory:"); con.executescript(pathlib.Path(out).read_text())
assert con.execute("SELECT claim, embedding FROM nodes").fetchone() == ("alias value", None)
assert con.execute("PRAGMA user_version").fetchone()[0] == 4888
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(SRC), str(tmp_path / "source.db"), str(tmp_path / "out.sql")],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_capability_probe_is_real_and_reports_this_interpreter(monkeypatch):
    result = engram_backup.probe_sql_backup_capability()
    assert result["available"] is True, result
    assert result["error"] is None
    assert result["python_version"] == ".".join(map(str, sys.version_info[:3]))
    assert result["interpreter"] == sys.executable
    assert result["iterator_kind"] in {"native", "stdlib-helper"}

    def incomplete_dump(src, out):
        Path(out).write_text("BEGIN TRANSACTION;\nCOMMIT;\n", encoding="utf-8")
        return {"iterator_kind": "native"}

    monkeypatch.setattr(engram_backup, "dump_stripped", incomplete_dump)
    bad = engram_backup.probe_sql_backup_capability()
    assert bad["available"] is False
    assert bad["error"]


@pytest.mark.parametrize(
    ("claim", "user_version"),
    [("wrong value", 4888), ("SQL backup capability probe", 0)],
)
def test_capability_probe_rejects_restorable_value_or_version_mismatch(monkeypatch, claim, user_version):
    def mismatched_dump(src, out):
        Path(out).write_text(
            "BEGIN TRANSACTION;\n"
            "CREATE TABLE nodes(id TEXT PRIMARY KEY, claim TEXT, embedding BLOB);\n"
            "INSERT INTO nodes VALUES ('ob_backup_probe', %r, NULL);\n" % claim +
            "COMMIT;\nPRAGMA user_version = %s;\n" % user_version,
            encoding="utf-8",
        )
        return {"iterator_kind": "native"}

    monkeypatch.setattr(engram_backup, "dump_stripped", mismatched_dump)
    result = engram_backup.probe_sql_backup_capability()
    assert result["available"] is False
    assert result["error"]


def test_health_attaches_negative_capability_even_when_graph_is_unavailable(monkeypatch, tmp_path):
    cap = {"available": False, "error": "named capability failure", "python_version": "3.x",
           "backend_sqlite_version": "x", "interpreter": "test", "iterator_kind": None}
    monkeypatch.setattr(health_data, "get_sql_backup_capability", lambda: cap)
    result = health_data.get_health_data(str(tmp_path / "missing.db"))
    assert result["backup_capability"] == cap
    assert "error" in result


def test_health_template_has_plain_text_capability_banner():
    template = (SRC / "viz" / "templates" / "health.html").read_text(encoding="utf-8")
    assert 'id="backup-capability-banner"' in template
    assert "SQL backup unavailable here" in template
    assert "textContent" in template
