"""engram-alpha#3941 -- the lesson-pattern audit reads the store through the shared owner-read helper.

Behind `engramd.lesson_pattern_audit_reads_via_owner` (default OFF) the tool's one lesson read goes to the
engramd owner; flag OFF opens the store `mode=ro` through `owner_read.open_reader`, which first classifies the
store path (`check_store_path`).  The three former direct `mode=ro` connects (the coverage audit, the view
comparison, `--write-ranks`) are now ONE function, `read_lesson_rows`.

Contract cells (numbers follow the dispatch spec):

  (c) store path     a FIFO / a directory as the store is refused by the classifier, promptly (no hang), exit 1,
                     the typed refusal on stderr, nothing written.
  (d) flag ON        the read goes to the owner (a fake one), gives the same ranks as the direct read, and sends
                     only registered templates.  Owner down: exit 1 naming the owner, no direct open, the
                     existing rank file untouched.
  registration       the read template is registered under the tool's own site and the scan config roots it.

Every cell names its mutation in the docstring.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner, registered_sql  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOL_LINK = REPO_ROOT / "tools" / "lesson-pattern-audit.py"
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))
import engramd_read_registry as rr  # noqa: E402

FLAG_KEY = "lesson_pattern_audit_reads_via_owner"
SITE_PREFIX = "tools/lesson-pattern-audit.py:"
TIMEOUT = 20

_SCHEMA = """CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 1,
 metadata TEXT NOT NULL DEFAULT '{}', claim TEXT NOT NULL DEFAULT '')"""


@pytest.fixture
def seat():
    """A short path: the owner socket lives at <home>/run/engramd.sock (AF_UNIX path cap)."""
    home = Path(tempfile.mkdtemp(prefix="lp-")).resolve()
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute(_SCHEMA)
    for nid, pat in (("lsx-001", "gh pr create"), ("lsx-002", "git status"), ("lsx-003", "never-matches")):
        conn.execute("INSERT INTO nodes (id, type, is_current, metadata, claim) VALUES (?, 'lesson', 1, ?, 'c')",
                     (nid, json.dumps({"situation_pattern": pat})))
    conn.execute("INSERT INTO nodes (id, type, is_current, metadata, claim) VALUES "
                 "('lsx-old', 'lesson', 0, ?, 'old')", (json.dumps({"situation_pattern": "ls"}),))
    conn.commit()
    conn.close()
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}
        for c in ("gh pr create a", "gh pr create b", "git status", "ls")]
    (home / "t.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _set_flag(home, value=True):
    (Path(home) / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


def _load(home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("lpa_3941_owner", TOOL_LINK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ranks(mod, home, out_name="ranks.json"):
    out = home / out_name
    rc = mod.run_write_ranks(db_path=str(home / "knowledge.db"), transcript_glob=str(home / "t.jsonl"),
                             path=str(out), out=open(os.devnull, "w"), err=open(os.devnull, "w"))
    return rc, (json.loads(out.read_text())["ranks"] if out.exists() else None)


def _cli(home, args, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env.update(extra)
    return subprocess.run([sys.executable, str(TOOL_LINK), *args], capture_output=True, text=True, env=env,
                          timeout=TIMEOUT)


class Opens:
    """Raise-and-record every way THIS thread could open a sqlite store."""

    def __init__(self):
        self.record = []
        self._thread = threading.get_ident()

    def install(self, monkeypatch):
        real = {"connect": sqlite3.connect, "dbapi2": sqlite3.dbapi2.connect}

        def guard(name):
            def f(*a, **k):
                if threading.get_ident() != self._thread:
                    return real[name](*a, **k)
                self.record.append((name, str(a[0]) if a else ""))
                raise AssertionError(f"direct open via {name}: {a[:1]}")
            return f

        monkeypatch.setattr(sqlite3, "connect", guard("connect"))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", guard("dbapi2"))
        return self


# ---- (c) a store path that is not a regular file ---------------------------------------------------

@pytest.mark.parametrize("mode", ["--write-ranks", None, "--compare-views"], ids=["write-ranks", "audit", "compare"])
@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_c_a_non_regular_store_path_is_refused_by_the_classifier_promptly(seat, kind, mode):
    """Mutation: a direct `sqlite3.connect(file:<path>?mode=ro)` in the tool instead of `open_reader` -> the FIFO
    open blocks (the run times out -> a FAILED cell, never a skip) and the directory is not classified."""
    bad = seat / "store-path"
    if kind == "fifo":
        os.mkfifo(bad)
    else:
        bad.mkdir()
    ranks = seat / "out.json"
    args = ["--db", str(bad), "--transcript-glob", str(seat / "t.jsonl")]
    if mode == "--write-ranks":
        args += ["--write-ranks", str(ranks)]
    elif mode:
        args += [mode]
    done = _cli(seat, args)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert ("is a FIFO" if kind == "fifo" else "is a directory") in done.stderr
    assert "not a regular file" in done.stderr
    assert not ranks.exists()


def test_c_a_missing_store_is_the_not_found_text_exit_1(seat):
    done = _cli(seat, ["--db", str(seat / "nope.db"), "--transcript-glob", str(seat / "t.jsonl"),
                       "--write-ranks", str(seat / "out.json")])
    assert done.returncode == 1
    assert "ENGRAM database not found" in done.stderr
    assert not (seat / "out.json").exists()


# ---- (d) flag ON -------------------------------------------------------------------------------------

def test_d_flag_off_and_flag_on_with_a_fake_owner_give_the_same_ranks(seat, monkeypatch):
    """Mutation: the flag-ON branch ignores the owner's rows (reads the store directly) -> the owner sees no
    `read` request -> red (see the last assertion); an owner that drops a row -> the ranks differ -> red."""
    _set_flag(seat, False)
    rc_off, off = _ranks(_load(seat, monkeypatch), seat, "off.json")
    assert rc_off == 0 and off == {"lsx-001": 500.0, "lsx-002": 250.0, "lsx-003": 0.0}
    _set_flag(seat, True)
    mod_on = _load(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        rc_on, on = _ranks(mod_on, seat, "on.json")
    assert rc_on == 0 and on == off
    assert any(r["method"] == "read" for r in owner.requests)


def test_d_flag_off_never_contacts_the_owner(seat, monkeypatch):
    _set_flag(seat, False)
    mod = _load(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        rc, ranks = _ranks(mod, seat)
    assert rc == 0 and ranks and owner.requests == []


def test_d_every_statement_a_flag_on_run_sends_is_a_registered_template_of_this_tool(seat, monkeypatch):
    """Mutation: a reworded statement in the tool (not re-registered) -> unregistered_template -> red."""
    _set_flag(seat, True)
    mod = _load(seat, monkeypatch)
    mine = {sql for sql, _s in _sites()}
    with FakeOwner(seat) as owner:
        rc, _ = _ranks(mod, seat)
    sent = [r["params"]["sql"] for r in owner.requests if r["method"] == "read"]
    assert rc == 0 and sent
    assert set(sent) <= mine, set(sent) - mine
    assert set(sent) <= registered_sql()


def test_d_flag_on_owner_down_exits_1_names_the_owner_never_opens_the_store_and_keeps_the_file(seat, monkeypatch):
    """Mutation: owner down falls back to a direct read, or writes empty ranks -> an open is recorded / the
    existing file changes -> red."""
    _set_flag(seat, True)
    mod = _load(seat, monkeypatch)
    out = seat / "ranks.json"
    out.write_bytes(b'{"ranks": {"keep": 1.0}}\n')
    opens = Opens().install(monkeypatch)
    with pytest.raises(mod.LessonAuditOwnerReadError) as exc:
        mod.run_write_ranks(db_path=str(seat / "knowledge.db"), transcript_glob=str(seat / "t.jsonl"),
                            path=str(out), out=open(os.devnull, "w"), err=open(os.devnull, "w"))
    assert "owner" in str(exc.value).lower() and "empty" in str(exc.value).lower()
    assert opens.record == [], opens.record
    assert out.read_bytes() == b'{"ranks": {"keep": 1.0}}\n'


def test_d_flag_on_owner_down_through_the_cli_is_exit_1_with_one_stderr_line(seat):
    _set_flag(seat, True)
    out = seat / "ranks.json"
    done = _cli(seat, ["--transcript-glob", str(seat / "t.jsonl"), "--write-ranks", str(out)])
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "engramd owner" in done.stderr
    assert not out.exists()


def test_d_flag_on_for_another_store_keeps_the_direct_path(seat, monkeypatch):
    """An explicit --db naming another store reads directly even with the flag ON (the helper's rule)."""
    other = Path(tempfile.mkdtemp(prefix="lp-other-")).resolve()
    try:
        shutil.copyfile(seat / "knowledge.db", other / "knowledge.db")
        _set_flag(seat, True)
        mod = _load(seat, monkeypatch)
        out = seat / "o.json"
        with FakeOwner(seat) as owner:
            rc = mod.run_write_ranks(db_path=str(other / "knowledge.db"), transcript_glob=str(seat / "t.jsonl"),
                                     path=str(out), out=open(os.devnull, "w"), err=open(os.devnull, "w"))
        assert rc == 0 and owner.requests == []
    finally:
        shutil.rmtree(other, ignore_errors=True)


# ---- registration ------------------------------------------------------------------------------------

def _sites():
    data = json.loads((SRC_ENGRAM / "engramd_read_templates.json").read_text())
    return [(t["sql"], s) for t in data["templates"] for s in t["sites"] if s.startswith(SITE_PREFIX)]


def test_t1_the_tool_has_a_registered_template_under_its_own_site():
    sites = _sites()
    assert sites, "no template is registered under tools/lesson-pattern-audit.py"
    assert all(sql.startswith("select") or sql.startswith("SELECT") for sql, _ in sites)


def test_t2_the_scan_config_names_the_tool_as_a_root():
    cfg = json.loads((REPO_ROOT / "tools" / "engramd_read_scan.config.json").read_text())
    assert any(r.startswith(SITE_PREFIX) for r in cfg["roots"])


def test_t3_the_registered_template_survives_registry_classification(seat):
    conn = sqlite3.connect(str(seat / "knowledge.db"))
    try:
        reg = rr.load_registry(SRC_ENGRAM / "engramd_read_templates.json", conn)
        for sql, _site in _sites():
            assert sql not in reg.refused, reg.refused.get(sql)
            assert hashlib.sha256(sql.encode("utf-8")).hexdigest() in reg.templates
    finally:
        conn.close()


def test_t4_the_tool_has_no_direct_sqlite_connect_of_its_own():
    """Mutation: a `sqlite3.connect(` line in the tool (a bypass of open_reader) -> red.  The read-scan
    --check and the #2652 ledger gate are the CI twins of this cell."""
    text = (SRC_ENGRAM / "tools" / "lesson-pattern-audit.py").read_text()
    assert "sqlite3.connect" not in text and "mode=ro" not in text


def test_the_test_map_names_this_file():
    maps = json.loads((SRC_ENGRAM / "packaging" / "test-map.json").read_text())["mappings"]
    entry = [e for e in maps if e.get("path_prefix_or_glob") == "tools/lesson-pattern-audit.py"]
    assert len(entry) == 1 and "tests/test_lesson_pattern_audit_owner_read_3941.py" in entry[0]["test_globs"]


def test_the_flag_key_is_the_one_the_tool_declares(seat, monkeypatch):
    mod = _load(seat, monkeypatch)
    assert mod.OWNER_FLAG_KEY == FLAG_KEY
