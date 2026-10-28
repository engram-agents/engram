"""Contract tests for #4205 part 1: a refused / failed off-disk push is loud.

Written before the implementation.  The snapshot worker persists the outcome of
every push attempt to ``<ENGRAM_HOME>/backup-push-status.json``; the SessionStart
hook, ``engram_checkpoint`` and ``engram_diagnose`` read it.  Every git-touching
cell uses tmp-dir repos and a bare tmp "remote"; the real ENGRAM_HOME is never
touched.
"""

import atexit
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
STATUS_NAME = "backup-push-status.json"
KEYS = {
    "last_attempt_utc", "last_success_utc", "last_commit_sha", "result",
    "stderr_tail", "ahead", "behind", "rewrite_guard",
}

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def sh(cwd, *args):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    sh(path, "init", "-q", "-b", "master")
    sh(path, "config", "user.email", "t@example.invalid")
    sh(path, "config", "user.name", "t")
    return path


class Home:
    """A tmp ENGRAM_HOME path plus its bare remote."""

    def __init__(self, path, bare):
        self.path, self.bare = path, bare

    def __truediv__(self, other):
        return self.path / other

    def __fspath__(self):
        return str(self.path)

    def __getattr__(self, name):
        return getattr(self.path, name)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp ENGRAM_HOME git repo with an upstream bare remote and one pushed commit."""
    import engram_core
    import engram_snapshot_worker  # noqa: F401

    bare = tmp_path / "remote.git"
    bare.mkdir()
    sh(bare, "init", "-q", "--bare", "-b", "master")
    h = init_repo(tmp_path / "home")
    (h / "config.json").write_text("{}\n")
    sh(h, "add", "config.json")
    sh(h, "commit", "-q", "-m", "base")
    sh(h, "remote", "add", "origin", str(bare))
    sh(h, "push", "-q", "-u", "origin", "master")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    monkeypatch.delenv("ENGRAM_ALLOW_HISTORY_REWRITE", raising=False)
    return Home(h, bare)


def commit_via_worker(home, n="1"):
    import engram_snapshot_worker as w
    (home / "config.json").write_text(json.dumps({"n": n}) + "\n")
    return w._commit_snapshot_files("nap", f"msg {n}")


def read_status(home):
    return json.loads((home / STATUS_NAME).read_text())


def diverge_remote(tmp_path, home):
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", str(home.bare), str(other))
    sh(other, "config", "user.email", "t@example.invalid")
    sh(other, "config", "user.name", "t")
    (other / "x.txt").write_text("x")
    sh(other, "add", "x.txt")
    sh(other, "commit", "-q", "-m", "remote only")
    sh(other, "push", "-q", "origin", "master")


# -- 1. persisted status ------------------------------------------------------
def test_status_file_shape_on_ok(home):
    r = commit_via_worker(home)
    assert r["git_committed"] is True
    st = read_status(home)
    assert set(st) == KEYS
    assert st["result"] == "ok"
    assert st["last_success_utc"] == st["last_attempt_utc"]
    assert st["last_commit_sha"] == sh(home.path, "rev-parse", "HEAD")
    assert (st["ahead"], st["behind"]) == (0, 0)
    assert len(st["stderr_tail"]) <= 300
    assert st["rewrite_guard"] in {"installed", "unsupported", "opt-out"}
    assert stat.S_IMODE((home / STATUS_NAME).stat().st_mode) == 0o600


def test_status_file_shape_on_refused(home, tmp_path):
    diverge_remote(tmp_path, home)
    commit_via_worker(home)
    st = read_status(home)
    assert set(st) == KEYS
    assert st["result"] == "refused"
    assert st["last_success_utc"] is None
    assert st["stderr_tail"] and len(st["stderr_tail"]) <= 300
    assert (st["ahead"], st["behind"]) == (1, 1)


def test_status_file_shape_on_error(home):
    hook = home.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
    hook.chmod(0o755)
    commit_via_worker(home)
    st = read_status(home)
    assert st["result"] == "error"
    assert set(st) == KEYS


def test_status_file_shape_on_no_remote(home):
    sh(home.path, "remote", "remove", "origin")
    commit_via_worker(home)
    st = read_status(home)
    assert st["result"] == "no-remote"
    assert st["ahead"] is None and st["behind"] is None
    assert set(st) == KEYS


def test_last_success_preserved_across_failed_attempt(home, tmp_path):
    commit_via_worker(home, "1")
    first = read_status(home)["last_success_utc"]
    assert first
    diverge_remote(tmp_path, home)
    commit_via_worker(home, "2")
    st = read_status(home)
    assert st["result"] == "refused"
    assert st["last_success_utc"] == first


def test_status_file_is_0600_after_overwrite_of_looser_file(home):
    p = home / STATUS_NAME
    p.write_text("{}")
    p.chmod(0o644)
    commit_via_worker(home)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_malformed_existing_status_is_overwritten_not_fatal(home):
    (home / STATUS_NAME).write_text("{not json")
    commit_via_worker(home)
    assert read_status(home)["result"] == "ok"


def test_no_tmp_file_left_behind(home):
    commit_via_worker(home)
    assert [p.name for p in home.path.iterdir() if p.name.startswith(STATUS_NAME)] == [STATUS_NAME]


def test_stderr_tail_capped_at_300(home):
    import engram_core
    engram_core._write_backup_push_status(
        result="error", stderr="x" * 1000, commit_sha="abc", ahead=None, behind=None
    )
    assert len(read_status(home)["stderr_tail"]) == 300


# -- 2. classification --------------------------------------------------------
@pytest.mark.parametrize("stderr,expected", [
    (" ! [rejected]        HEAD -> master (non-fast-forward)", "refused"),
    ("error: failed to push some refs\nhint: ... non-fast-forward", "refused"),
    (" ! [rejected] master -> master (fetch first)", "refused"),
    ("hint: Updates were rejected because the remote contains work (fetch first)", "refused"),
    ("fatal: No configured push destination.", "no-remote"),
    ("fatal: 'origin' does not appear to be a git repository", "error"),
    ("fatal: '/nonexistent/path' does not appear to be a git repository\nfatal: Could not read from remote repository.", "error"),
    ("fatal: unable to access 'https://x.invalid/': Could not resolve host", "error"),
    (" ! [remote rejected] master -> master (pre-receive hook declined)", "error"),
    ("remote: error: File x.bak-1 is 157.00 MB; this exceeds GitHub's file size limit of 100.00 MB", "rejected-size"),
    ("remote: error: GH001: Large files detected.\n ! [remote rejected] master -> master (pre-receive hook declined)",
     "rejected-size"),
])
def test_classification_by_stderr(stderr, expected):
    import engram_core
    assert engram_core._classify_push(1, stderr, True) == expected


def test_classification_ok_on_returncode_zero():
    import engram_core
    assert engram_core._classify_push(0, "To x\n   a..b  HEAD -> master", True) == "ok"


def test_classification_no_remote_when_remote_list_empty():
    import engram_core
    assert engram_core._classify_push(1, "whatever", False) == "no-remote"


# -- 2b. canonical .gitignore (amendment) -------------------------------------
def test_fresh_home_gitignore_ignores_secrets_and_dated_baks(tmp_path, monkeypatch):
    import engram_core
    h = init_repo(tmp_path / "fresh")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    engram_core._ensure_engram_gitignore(h)
    for name in ("secrets/x", "anthropic-api-key.foo", "knowledge.db.bak-2026-01-01"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=h)
        assert r.returncode == 0, f"{name} is not ignored"
    r = subprocess.run(["git", "check-ignore", "-q", "config.json"], cwd=h)
    assert r.returncode == 1  # the backup set stays tracked


def test_fresh_home_gitignore_ignores_4208_backup_copies(tmp_path, monkeypatch):
    """#4208: backups/, dated *.db.<suffix> copies and db-backup/binary/ never get staged."""
    import engram_core
    h = init_repo(tmp_path / "fresh4208")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    engram_core._ensure_engram_gitignore(h)
    for name in ("backups/x.db", "knowledge.db.2026-08-03-0045", "db-backup/binary/knowledge-x.db"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=h)
        assert r.returncode == 0, f"{name} is not ignored"


def test_existing_gitignore_gains_new_patterns_and_keeps_negations(tmp_path, monkeypatch):
    import engram_core
    h = init_repo(tmp_path / "old")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    old = [p for p in engram_core.ENGRAM_GITIGNORE_PATTERNS
           if p not in ("secrets/", "*api-key*", "*.bak-*")]
    (h / ".gitignore").write_text("\n".join(old) + "\n!diary/diary.db\n")
    engram_core._ensure_engram_gitignore(h)
    lines = (h / ".gitignore").read_text().splitlines()
    for p in ("secrets/", "*api-key*", "*.bak-*", "!diary/diary.db"):
        assert p in lines


# -- 3. SessionStart line -----------------------------------------------------
def load_hook(engram_home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)
    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_status(h, **over):
    st = {
        "last_attempt_utc": "2026-10-26T12:00:00Z", "last_success_utc": "2026-09-03T20:52:02Z",
        "last_commit_sha": "abc", "result": "refused", "stderr_tail": "! [rejected] non-fast-forward",
        "ahead": 827, "behind": 1, "rewrite_guard": "installed",
    }
    st.update(over)
    (Path(h) / STATUS_NAME).write_text(json.dumps(st))


def test_hook_line_present_when_not_ok(tmp_path, monkeypatch):
    mod = load_hook(tmp_path, monkeypatch)
    write_status(tmp_path)
    line = mod._backup_push_health_line(str(tmp_path))
    assert line.startswith("⚠️  ENGRAM off-disk backup: refused since 2026-09-03T20:52:02Z; ahead 827 behind 1; ")
    assert "\n" not in line


def test_hook_line_says_never_when_no_success(tmp_path, monkeypatch):
    mod = load_hook(tmp_path, monkeypatch)
    write_status(tmp_path, last_success_utc=None, result="error")
    assert "error since never;" in mod._backup_push_health_line(str(tmp_path))


def test_hook_line_stderr_bounded_to_120(tmp_path, monkeypatch):
    mod = load_hook(tmp_path, monkeypatch)
    write_status(tmp_path, stderr_tail="a" * 200 + "b" * 120)
    line = mod._backup_push_health_line(str(tmp_path))
    # the LAST 120 characters: git puts the reason at the end of its stderr
    assert line.endswith("b" * 120) and "a" not in line.split("behind 1; ", 1)[1]


def test_hook_line_absent_on_no_remote(tmp_path, monkeypatch):
    """No remote is a configuration state, not a failure."""
    mod = load_hook(tmp_path, monkeypatch)
    write_status(tmp_path, result="no-remote", ahead=None, behind=None)
    assert mod._backup_push_health_line(str(tmp_path)) is None


def test_hook_line_present_on_rejected_size(tmp_path, monkeypatch):
    mod = load_hook(tmp_path, monkeypatch)
    write_status(tmp_path, result="rejected-size")
    assert "rejected-size since" in mod._backup_push_health_line(str(tmp_path))


def test_hook_line_absent_on_missing_file(tmp_path, monkeypatch):
    mod = load_hook(tmp_path, monkeypatch)
    assert mod._backup_push_health_line(str(tmp_path)) is None


def test_hook_line_absent_on_ok(tmp_path, monkeypatch):
    mod = load_hook(tmp_path, monkeypatch)
    write_status(tmp_path, result="ok", ahead=0, behind=0)
    assert mod._backup_push_health_line(str(tmp_path)) is None


@pytest.mark.parametrize("content", ["{not json", "[]", '"s"'])
def test_hook_line_unreadable_form(tmp_path, monkeypatch, content):
    mod = load_hook(tmp_path, monkeypatch)
    (tmp_path / STATUS_NAME).write_text(content)
    assert mod._backup_push_health_line(str(tmp_path)) == "⚠️  ENGRAM off-disk backup: status file unreadable"


def _run_main(mod, tmp_path):
    stdin = json.dumps({"session_id": "s4205", "transcript_path": str(tmp_path / "t.jsonl"), "source": "startup"})
    buf = io.StringIO()
    mod.detect_harness = lambda: "claude-code"
    mod.tool_capability_index_block = lambda: ""
    client = MagicMock()
    client.call.return_value = {}
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=MagicMock(return_value=client))}):
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()))):
            with redirect_stdout(buf):
                mod.main()
    return json.loads(buf.getvalue().strip())["hookSpecificOutput"]["additionalContext"]


def test_hook_main_emits_line_through_emitter_iff_not_ok(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    write_status(tmp_path)
    ctx = _run_main(load_hook(tmp_path, monkeypatch), tmp_path)
    assert ctx.count("ENGRAM off-disk backup: refused") == 1
    (tmp_path / STATUS_NAME).unlink()
    ctx = _run_main(load_hook(tmp_path, monkeypatch), tmp_path)
    assert "off-disk backup" not in ctx
    write_status(tmp_path, result="ok")
    ctx = _run_main(load_hook(tmp_path, monkeypatch), tmp_path)
    assert "off-disk backup" not in ctx


# -- 4/5. server surfaces -----------------------------------------------------
TEST_DIR = tempfile.mkdtemp(prefix="engram_test_backup_push_4205_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    # The checkpoint enqueues an async snapshot job whose worker writes into TEST_DIR
    # (incl. the push-status file); stop it before wiping so a lagging worker can neither
    # race the rmtree nor leak a status file into the next test's fresh directory.
    prior = sys.modules.get("engram_snapshot_worker")
    if prior is not None:
        prior.stop_worker(timeout=10.0)
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def silence_snapshot_worker(monkeypatch):
    """A checkpoint enqueues a real async snapshot job; its worker would write its own
    push status (`no-remote`) into TEST_DIR and race the file state a cell set up.
    No-op the commit so the cell's file is the only writer."""
    import engram_snapshot_worker as w
    monkeypatch.setattr(w, "_commit_snapshot_files",
                        lambda *a, **k: {"git_committed": False, "reason": "test: worker silenced"})


def test_checkpoint_carries_backup_push_equal_to_file(monkeypatch):
    server = fresh_server()
    silence_snapshot_worker(monkeypatch)
    write_status(TEST_DIR, last_success_utc="2026-10-01T00:00:00Z", ahead=3, behind=2)
    r = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "c4205"})))
    assert r["backup_push"] == {
        "result": "refused", "last_success_utc": "2026-10-01T00:00:00Z", "ahead": 3, "behind": 2,
    }


def test_checkpoint_backup_push_null_when_file_absent(monkeypatch):
    server = fresh_server()
    silence_snapshot_worker(monkeypatch)
    r =json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "c4205b"})))
    assert "backup_push" in r and r["backup_push"] is None


def diagnose_with(**over):
    server = fresh_server()
    base = {"result": "ok", "last_success_utc": None, "ahead": 0, "behind": 0,
            "last_attempt_utc": iso(datetime.now(timezone.utc))}
    base.update(over)
    write_status(TEST_DIR, **base)
    return json.loads(server.engram_diagnose())["backup_push"]


def test_diagnose_flare_true_on_refused_older_than_24h():
    old = iso(datetime.now(timezone.utc) - timedelta(hours=25))
    bp = diagnose_with(result="refused", last_success_utc=old, ahead=2)
    assert bp["flare"] is True
    assert set(bp) == {"result", "last_success_utc", "last_attempt_utc", "ahead", "behind", "flare"}


def test_diagnose_flare_true_on_refused_never_succeeded():
    assert diagnose_with(result="refused", last_success_utc=None)["flare"] is True


def test_diagnose_flare_false_on_ok():
    now = iso(datetime.now(timezone.utc))
    assert diagnose_with(result="ok", last_success_utc=now)["flare"] is False


def test_diagnose_flare_false_on_refused_younger_than_24h():
    recent = iso(datetime.now(timezone.utc) - timedelta(hours=1))
    assert diagnose_with(result="refused", last_success_utc=recent)["flare"] is False


def test_diagnose_flare_true_on_behind_regardless():
    now = iso(datetime.now(timezone.utc))
    assert diagnose_with(result="ok", last_success_utc=now, behind=1)["flare"] is True


def test_diagnose_backup_push_present_without_file():
    server = fresh_server()
    bp = json.loads(server.engram_diagnose())["backup_push"]
    assert bp["flare"] is False and bp["result"] is None


def test_diagnose_backup_push_unreadable_file_flares():
    server = fresh_server()
    (Path(TEST_DIR) / STATUS_NAME).write_text("{bad")
    bp = json.loads(server.engram_diagnose())["backup_push"]
    assert bp["result"] == "unreadable" and bp["flare"] is True


def _diagnose_archive():
    return json.loads(fresh_server_diag.engram_diagnose())["backup_archive"]


def test_diagnose_backup_archive_counts_and_dates_4208():
    global fresh_server_diag
    fresh_server_diag = fresh_server()
    bk = Path(TEST_DIR) / "db-backup"
    (bk / "binary").mkdir(parents=True)
    for n, size in (("knowledge-2026-09-01.sql", 10), ("knowledge-2026-10-05.sql", 20),
                    ("knowledge-2026-10-05-2.sql", 30)):
        (bk / n).write_bytes(b"x" * size)
    (bk / "binary" / "knowledge-2026-10-05T010203Z.db").write_bytes(b"y" * 7)
    ba = _diagnose_archive()
    assert ba == {
        "sql_count": 3, "sql_bytes": 60, "binary_count": 1, "binary_bytes": 7,
        "tracked_backup_count": 0, "oldest": "2026-09-01", "newest": "2026-10-05", "flare": False,
    }


def test_diagnose_backup_archive_empty_dir_zeros_and_nulls_4208():
    global fresh_server_diag
    fresh_server_diag = fresh_server()
    ba = _diagnose_archive()
    assert ba == {
        "sql_count": 0, "sql_bytes": 0, "binary_count": 0, "binary_bytes": 0,
        "tracked_backup_count": 0, "oldest": None, "newest": None, "flare": False,
    }


def test_diagnose_backup_archive_tracked_binary_flares_4208():
    global fresh_server_diag
    fresh_server_diag = fresh_server()
    h = init_repo(Path(TEST_DIR))
    (h / "knowledge.db.2026-08-03-0045").write_bytes(b"z")
    sh(h, "add", "-f", "knowledge.db.2026-08-03-0045")
    ba = _diagnose_archive()
    assert ba["tracked_backup_count"] == 1 and ba["flare"] is True
    assert "tracked_binary_count" not in ba


def test_diagnose_backup_archive_error_shape_never_absent_4208(monkeypatch):
    global fresh_server_diag
    fresh_server_diag = fresh_server()
    import engram_core

    def boom(_home):
        raise RuntimeError("nope")

    monkeypatch.setattr(engram_core, "_tracked_binary_paths", boom)
    ba = _diagnose_archive()
    assert ba == {"error": "RuntimeError: nope"}


def test_diagnose_impl_itself_carries_backup_archive_4208():
    """R2-2: the key is built in engram_stats._diagnose_impl, not in a server.py wrapper."""
    import engram_stats
    fresh_server()
    out = json.loads(engram_stats._diagnose_impl())
    assert "backup_archive" in out
    assert "backup_push" in out


def test_diagnose_no_remote_reported_but_never_flares():
    """An install without a remote is a configuration state, not a failure."""
    bp = diagnose_with(result="no-remote", last_success_utc=None, ahead=None, behind=None)
    assert bp["result"] == "no-remote" and bp["flare"] is False


def test_diagnose_rejected_size_flares_when_never_succeeded():
    assert diagnose_with(result="rejected-size", last_success_utc=None)["flare"] is True


# -- 8a. a snapshot that makes no push attempt leaves the status file untouched ----
def test_no_change_snapshot_leaves_status_byte_identical(home):
    commit_via_worker(home)
    before = (home / STATUS_NAME).read_bytes()
    r = commit_via_worker_unchanged(home)
    assert r["git_committed"] is False
    assert (home / STATUS_NAME).read_bytes() == before


def commit_via_worker_unchanged(home):
    import engram_snapshot_worker as w
    return w._commit_snapshot_files("nap", "again")


def test_commit_failure_leaves_status_byte_identical(home):
    commit_via_worker(home, "1")
    before = (home / STATUS_NAME).read_bytes()
    hook = home / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    r = commit_via_worker(home, "2")
    assert r["git_committed"] is False and "commit failed" in r["reason"]
    assert (home / STATUS_NAME).read_bytes() == before


def test_add_failure_leaves_status_byte_identical(home):
    commit_via_worker(home, "1")
    before = (home / STATUS_NAME).read_bytes()
    (home / ".git" / "index.lock").write_text("")
    r = commit_via_worker(home, "2")
    assert r["git_committed"] is False and "add failed" in r["reason"]
    assert (home / STATUS_NAME).read_bytes() == before


def test_diagnose_binary_count_uses_rotation_regex_4208():
    """R3-5: a foreign other.db in binary/ is not counted."""
    global fresh_server_diag
    fresh_server_diag = fresh_server()
    bk = Path(TEST_DIR) / "db-backup"
    (bk / "binary").mkdir(parents=True)
    (bk / "binary" / "knowledge-2026-10-05T010203Z.db").write_bytes(b"y" * 7)
    (bk / "binary" / "other.db").write_bytes(b"z" * 100)
    ba = _diagnose_archive()
    assert ba["binary_count"] == 1 and ba["binary_bytes"] == 7
