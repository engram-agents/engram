"""engram-alpha#4445: bootstrap asks before touching ~/.claude/settings.json, never
replaces a corrupt one, and keeps the ENGRAM data home owner-only.

Contract (cells written before the implementation):

C1  Retention is asked, not set.
    * `--retention-days N` (1..36500) is the ONLY way bootstrap touches settings.json.
      No flag: the file is byte-identical (bytes AND mtime_ns) and an absent file stays absent.
    * With the flag: absent -> created {"cleanupPeriodDays": N}; valid object without the key ->
      merged; with the key -> kept byte-identical and one line says so. Atomic write, no temp left.
    * Bad values (abc, 0, -5, 36501, missing) are refused naming the flag and the range.
C2  A settings.json that is not a JSON object is backed up (settings.json.corrupt.<UTC>) and the run
    refuses; the file is never replaced. Dangling symlink / unreadable file refuse; a same-second
    backup is left untouched. The refusal comes AFTER the data-home tighten step.
C3  `engram_home_perms.tighten_data_home(home)`: home and the named data dirs 0700, files without
    group/other bits, top-level regular files likewise; never follows or descends symlinks; never
    touches .git / marketplace / venv / tools; ACL-aware on Linux; idempotent.

Runs use tmp homes through env; the real ~/.claude and ~/.engram are never read.
"""

from __future__ import annotations

import errno as _errno
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
BOOTSTRAP = SRC / "bootstrap.py"

RUNTIME_FILES = [
    "server.py", "engram_client.py", "engram_confidence.py",
    "engram_filter.py", "engram_idf.py", "engram_ids.py",
    "engram_log_emitter.py", "engram_log_indexer.py",
]

SKIP_ROOTS = (".git", "marketplace", "venv", "tools")
# Every directory shipped code creates under the home that holds private data. config-backup
# was dropped: no shipped code creates it.
DATA_DIRS = (
    "diary", "sessions", "compact", "history", "health-snapshots",
    "session-logs-archive", "claude-projects-archive", "db-backup", "evidence-archive",
    "logs", "telemetry", "session-start-overflow", "tripwire-overflow",
)


def _run(tmp_path: Path, args=None, *, umask=0o022, target="claude"):
    engram_home = tmp_path / ".engram"
    engram_home.mkdir(parents=True, exist_ok=True)
    os.chmod(engram_home, 0o777 & ~umask)
    for fname in RUNTIME_FILES:
        if (SRC / fname).exists():
            shutil.copy2(SRC / fname, engram_home / fname)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(engram_home),
        "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "PYTHON_BIN": sys.executable,
        "TARGET": target,
        "ENGRAM_NO_EMBEDDINGS": "1",
    })
    for k in ("ENGRAM_BOOTSTRAP", "FORCE", "FORCE_RESEED_EMPTY"):
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, str(BOOTSTRAP), *(args or [])],
        env=env, capture_output=True, text=True, timeout=120,
        preexec_fn=lambda: os.umask(umask),
    )


def _settings(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def _seed_settings(tmp_path: Path, data: bytes) -> Path:
    p = _settings(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    return p


def _snap(p: Path):
    st = os.lstat(p)
    return p.read_bytes(), st.st_mtime_ns, stat.S_IMODE(st.st_mode)


def _backups(tmp_path: Path):
    return sorted((tmp_path / ".claude").glob("settings.json.corrupt.*"))


# ===================================================================== C1

@pytest.mark.parametrize("pre", [None, b'{"model": "sonnet"}\n', b"{not json"])
def test_c1a_no_flag_never_touches_settings(tmp_path, pre):
    before = None
    if pre is not None:
        p = _seed_settings(tmp_path, pre)
        before = _snap(p)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    if pre is None:
        assert not _settings(tmp_path).exists()
    else:
        assert _snap(_settings(tmp_path)) == before
    assert not _backups(tmp_path)


def test_c1b_flag_creates_absent_file_mode_0600(tmp_path):
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(_settings(tmp_path).read_text()) == {"cleanupPeriodDays": 90}
    assert stat.S_IMODE(os.stat(_settings(tmp_path)).st_mode) == 0o600
    out = r.stdout
    assert "90" in out and "days" in out and "transcript" in out


def test_c1b_flag_merges_preserving_keys_indent_and_mode(tmp_path):
    p = _seed_settings(tmp_path, b'{\n    "model": "sonnet",\n    "theme": "dark"\n}\n')
    os.chmod(p, 0o640)
    r = _run(tmp_path, ["--retention-days", "365"])
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(p.read_text())
    assert data == {"model": "sonnet", "theme": "dark", "cleanupPeriodDays": 365}
    assert '\n    "model"' in p.read_text(), "the file's own 4-space indent is kept"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640


def test_c1b_flag_with_existing_key_keeps_bytes_and_says_so(tmp_path):
    p = _seed_settings(tmp_path, b'{"cleanupPeriodDays": 30}\n')
    before = _snap(p)
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _snap(p) == before
    lines = [ln for ln in r.stdout.splitlines() if "cleanupPeriodDays" in ln and "kept" in ln]
    assert len(lines) == 1, r.stdout
    assert "30" in lines[0] and str(p) in lines[0]


@pytest.mark.parametrize("bad", [["abc"], ["0"], ["-5"], ["36501"], []])
def test_c1c_bad_values_refused_naming_flag_and_range(tmp_path, bad):
    r = _run(tmp_path, ["--retention-days", *bad])
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "--retention-days" in out and "36500" in out
    assert not _settings(tmp_path).exists()


@pytest.mark.parametrize("order", [
    ["--force", "--retention-days", "90"], ["--retention-days", "90", "--force"],
])
def test_c1c_force_combos(tmp_path, order):
    r = _run(tmp_path, order)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(_settings(tmp_path).read_text())["cleanupPeriodDays"] == 90


def test_c1c_unknown_argument_still_refused(tmp_path):
    r = _run(tmp_path, ["--retention-days", "90", "--bogus"])
    assert r.returncode != 0
    assert "--bogus" in r.stdout + r.stderr
    assert not _settings(tmp_path).exists()


def test_c1d_atomic_write_leaves_no_temp_sibling(tmp_path):
    _seed_settings(tmp_path, b'{"model": "x"}\n')
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode == 0, r.stdout + r.stderr
    names = {p.name for p in (tmp_path / ".claude").iterdir()}
    stray = [n for n in names if n != "settings.json" and "settings" in n]
    # The merge (MERGE class) takes ONE stamped backup of the file before changing it; nothing else.
    assert len(stray) == 1 and ".engram-backup." in stray[0], stray
    assert (tmp_path / ".claude" / stray[0]).read_bytes() == b'{"model": "x"}\n'
    assert not [n for n in names if "tmp" in n.lower() or n.endswith(".partial")]


# ===================================================================== C2

@pytest.mark.parametrize("content", [b"{not json", b"", b"[1, 2]", b"42", b'"text"', b"null"])
def test_c2_non_object_settings_backed_up_and_refused(tmp_path, content):
    p = _seed_settings(tmp_path, content)
    before = _snap(p)
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "REFUSED" in out
    assert _snap(p) == before, "settings.json must stay byte-identical"
    backups = _backups(tmp_path)
    assert len(backups) == 1, backups
    assert backups[0].read_bytes() == content
    assert str(backups[0]) in out
    assert not [x for x in (tmp_path / ".claude").iterdir() if x.suffix == ".tmp"]


def test_c2_dangling_symlink_refused_no_backup(tmp_path):
    p = _settings(tmp_path)
    p.parent.mkdir(parents=True)
    os.symlink(tmp_path / "nowhere.json", p)
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode != 0
    assert "REFUSED" in r.stdout + r.stderr
    assert os.path.islink(p) and not os.path.exists(p)
    assert not (tmp_path / "nowhere.json").exists()
    assert not _backups(tmp_path)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode 000")
def test_c2_unreadable_file_refused_with_os_error_no_backup(tmp_path):
    p = _seed_settings(tmp_path, b'{"a": 1}\n')
    os.chmod(p, 0o000)
    try:
        r = _run(tmp_path, ["--retention-days", "90"])
    finally:
        os.chmod(p, 0o600)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "REFUSED" in out and ("Permission denied" in out or "Errno" in out)
    assert not _backups(tmp_path)
    assert p.read_bytes() == b'{"a": 1}\n'


def _load_bootstrap(tmp_path: Path):
    """Import bootstrap in-process under tmp env (for the same-second cell only)."""
    env = {
        "ALPHA_DIR": str(REPO_ROOT), "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(tmp_path / ".engram"), "CLAUDE_HOME": str(tmp_path / ".claude"),
        "PYTHON_BIN": sys.executable, "TARGET": "claude", "ENGRAM_NO_EMBEDDINGS": "1",
    }
    (tmp_path / ".engram").mkdir(exist_ok=True)
    return env


def test_c2_same_second_backup_left_untouched_and_run_still_refuses(tmp_path):
    """Drive the real settings step in a subprocess with the UTC stamp pinned."""
    content = b"{broken"
    p = _seed_settings(tmp_path, content)
    stamp = "20300101T000000Z"
    pre = tmp_path / ".claude" / f"settings.json.corrupt.{stamp}"
    pre.write_bytes(b"EARLIER BACKUP")
    code = (
        "import sys, runpy\n"
        f"sys.argv = [{str(BOOTSTRAP)!r}, '--retention-days', '90']\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('bootstrap_4445', {str(BOOTSTRAP)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"m._utc_stamp = lambda: {stamp!r}\n"
        "m.main()\n"
    )
    env = os.environ.copy()
    env.update(_load_bootstrap(tmp_path))
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "REFUSED" in r.stdout + r.stderr
    assert pre.read_bytes() == b"EARLIER BACKUP"
    assert p.read_bytes() == content


def test_c2_ordering_tighten_ran_even_though_final_step_refused(tmp_path):
    """The preflight now catches a corrupt settings.json before anything is written, so no
    plain input reaches the final step with a corrupt file. The ordering is still pinned for
    the one way it can happen: the file turns corrupt AFTER the preflight. Simulated by
    neutralising the preflight in-process; the final step must refuse AFTER the tighten step."""
    _seed_settings(tmp_path, b"{broken")
    code = (
        "import sys, importlib.util\n"
        f"sys.argv = [{str(BOOTSTRAP)!r}, '--retention-days', '90']\n"
        f"spec = importlib.util.spec_from_file_location('bootstrap_4445o', {str(BOOTSTRAP)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "m._preflight_settings = lambda *a, **k: None\n"
        "m.main()\n"
    )
    env = os.environ.copy()
    env.update(_load_bootstrap(tmp_path))
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       timeout=120, preexec_fn=lambda: os.umask(0o022))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "REFUSED" in r.stdout + r.stderr
    home = tmp_path / ".engram"
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "knowledge.db").st_mode) == 0o600


@pytest.mark.parametrize("content", [b"{not json", b"", b"[1]"])
def test_c2_preflight_refuses_before_anything_is_written(tmp_path, content):
    p = _seed_settings(tmp_path, content)
    before = _snap(p)
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode != 0
    assert "REFUSED" in r.stdout + r.stderr
    home = tmp_path / ".engram"
    assert not (home / "knowledge.db").exists()
    assert not (home / "first-run-pending").exists()
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert sorted(x.name for x in home.iterdir() if not x.name.endswith(".py")) == []
    assert _snap(p) == before
    assert len(_backups(tmp_path)) == 1 and _backups(tmp_path)[0].read_bytes() == content


def test_c2_repair_then_rerun_with_same_flag_succeeds(tmp_path):
    p = _seed_settings(tmp_path, b"{broken")
    assert _run(tmp_path, ["--retention-days", "90"]).returncode != 0
    p.write_text('{"model": "x"}\n')
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(p.read_text())["cleanupPeriodDays"] == 90
    assert (tmp_path / ".engram" / "knowledge.db").exists()


def test_c1_codex_target_says_retention_flag_is_ignored(tmp_path):
    p = _seed_settings(tmp_path, b'{"model": "x"}\n')
    before = _snap(p)
    r = _run(tmp_path, ["--retention-days", "90"], target="codex")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [ln for ln in r.stdout.splitlines() if "--retention-days" in ln]
    assert len(lines) == 1 and "Claude" in lines[0] and "ignored" in lines[0], r.stdout
    assert _snap(p) == before


# ===================================================================== C3

def _perms():
    spec = importlib.util.spec_from_file_location("engram_home_perms_4445", SRC / "engram_home_perms.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the defining module up here
    spec.loader.exec_module(mod)
    return mod


def _in_scope(home: Path):
    """Every path the tighten rule covers: home, named data dirs recursively, top-level files."""
    out = [home]
    for child in sorted(home.iterdir()):
        if child.name in SKIP_ROOTS or child.is_symlink():
            continue
        if child.is_file():
            out.append(child)
        elif child.is_dir() and child.name in DATA_DIRS:
            out.append(child)
            for dp, dns, fns in os.walk(child):
                for n in dns + fns:
                    q = Path(dp) / n
                    if not q.is_symlink():
                        out.append(q)
    return out


def _assert_tight(home: Path):
    paths = _in_scope(home)
    assert len(paths) > 3
    for p in paths:
        mode = stat.S_IMODE(os.lstat(p).st_mode)
        if p.is_dir():
            assert mode == 0o700, f"{p} is {oct(mode)}"
        else:
            assert mode & 0o077 == 0, f"{p} is {oct(mode)}"


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_c3_fresh_bootstrap_home_is_owner_only(tmp_path, umask):
    r = _run(tmp_path, umask=umask)
    assert r.returncode == 0, r.stdout + r.stderr
    home = tmp_path / ".engram"
    # Give the data dirs content too: they may not exist after bootstrap alone.
    _assert_tight(home)
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "knowledge.db").st_mode) == 0o600
    if (home / "config.json").exists():
        assert stat.S_IMODE(os.stat(home / "config.json").st_mode) == 0o600


def _loose_tree(home: Path):
    old = os.umask(0)
    try:
        home.mkdir(exist_ok=True)
        os.chmod(home, 0o755)
        for d in DATA_DIRS:
            (home / d / "sub").mkdir(parents=True)
            (home / d / "f.txt").write_text("x")
            (home / d / "sub" / "g.txt").write_text("y")
            os.chmod(home / d, 0o755)
            os.chmod(home / d / "sub", 0o755)
            os.chmod(home / d / "f.txt", 0o644)
            os.chmod(home / d / "sub" / "g.txt", 0o644)
        (home / "knowledge.db").write_bytes(b"db")
        (home / "knowledge.db-wal").write_bytes(b"w")
        (home / "config.json").write_text("{}")
        (home / "ledger.log").write_text("l")
        (home / "run.sh").write_text("#!/bin/sh\n")
        for n in ("knowledge.db", "knowledge.db-wal", "config.json", "ledger.log"):
            os.chmod(home / n, 0o644)
        os.chmod(home / "run.sh", 0o755)
    finally:
        os.umask(old)


def test_c3_loose_tree_tightened_exec_bit_kept_and_idempotent(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    mod = _perms()
    res = mod.tighten_data_home(home)
    assert res.errors == []
    assert "." in res.changed, "home itself is reported as '.'"
    _assert_tight(home)
    assert stat.S_IMODE(os.stat(home / "run.sh").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "knowledge.db").st_mode) == 0o600
    assert "knowledge.db" in res.changed and "diary/f.txt" in res.changed
    again = mod.tighten_data_home(home)
    assert again.changed == [] and again.errors == []


def test_c3_second_call_does_not_chmod_when_already_tight(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _loose_tree(home)
    mod = _perms()
    mod.tighten_data_home(home)
    calls = []
    real = os.chmod
    monkeypatch.setattr(os, "chmod", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    assert mod.tighten_data_home(home).changed == []
    assert calls == [], "a tight tree must cost zero chmod calls"


def test_c3_new_loose_dir_fixed_by_next_call(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    mod = _perms()
    mod.tighten_data_home(home)
    old = os.umask(0)
    try:
        (home / "sessions" / "newdir").mkdir()
        (home / "sessions" / "newdir" / "s.json").write_text("{}")
        os.chmod(home / "sessions" / "newdir" / "s.json", 0o666)
    finally:
        os.umask(old)
    res = mod.tighten_data_home(home)
    assert "sessions/newdir" in res.changed and "sessions/newdir/s.json" in res.changed
    _assert_tight(home)


def test_c3_data_dirs_set_is_pinned():
    mod = _perms()
    assert set(mod.DATA_DIRS) == set(DATA_DIRS)
    assert not set(mod.DATA_DIRS) & set(mod.NEVER_TOUCH)
    assert set(mod.NEVER_TOUCH) == {".git", "marketplace", "venv", "tools"}


@pytest.mark.parametrize("name", [
    "session-logs-archive", "claude-projects-archive", "db-backup", "evidence-archive",
    "logs", "telemetry", "session-start-overflow", "tripwire-overflow",
])
def test_c3_archive_dir_with_nested_content_fully_tightened(tmp_path, name):
    home = tmp_path / "h"
    _loose_tree(home)
    old = os.umask(0)
    try:
        (home / name / "a" / "b").mkdir(parents=True, exist_ok=True)
        (home / name / "a" / "b" / "t.jsonl").write_text("x")
        os.chmod(home / name, 0o755)
        os.chmod(home / name / "a", 0o755)
        os.chmod(home / name / "a" / "b", 0o755)
        os.chmod(home / name / "a" / "b" / "t.jsonl", 0o644)
    finally:
        os.umask(old)
    res = _perms().tighten_data_home(home)
    assert res.errors == []
    for rel in (name, f"{name}/a", f"{name}/a/b", f"{name}/a/b/t.jsonl"):
        assert rel in res.changed, rel
    _assert_tight(home)
    assert stat.S_IMODE(os.stat(home / name / "a" / "b" / "t.jsonl").st_mode) == 0o600


def test_c3_symlink_inside_archive_dir_untouched(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    os.chmod(outside, 0o644)
    (home / "session-logs-archive").mkdir(exist_ok=True)
    os.symlink(outside, home / "session-logs-archive" / "link.jsonl")
    _perms().tighten_data_home(home)
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o644


def test_c3_symlinks_never_followed_or_changed(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    os.chmod(outside, 0o644)
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    os.chmod(outdir, 0o755)
    (outdir / "inner.txt").write_text("i")
    os.chmod(outdir / "inner.txt", 0o644)
    os.symlink(outside, home / "diary" / "link.txt")
    os.symlink(outdir, home / "sessions" / "linkdir")
    os.symlink(outside, home / "toplink")
    res = _perms().tighten_data_home(home)
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(outdir).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(outdir / "inner.txt").st_mode) == 0o644
    assert os.path.islink(home / "diary" / "link.txt")
    assert not any("link" in c for c in res.changed)


def test_c3_git_marketplace_venv_tools_untouched(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    outdir = tmp_path / "plugin_tools"
    outdir.mkdir()
    (outdir / "t.py").write_text("t")
    os.chmod(outdir / "t.py", 0o644)
    os.symlink(outdir, home / "tools")
    before = {}
    for name in (".git", "marketplace", "venv"):
        (home / name / "deep").mkdir(parents=True)
        (home / name / "deep" / "f").write_text("f")
        os.chmod(home / name, 0o755)
        os.chmod(home / name / "deep", 0o755)
        os.chmod(home / name / "deep" / "f", 0o644)
    for dp, dns, fns in os.walk(home):
        if any(s in Path(dp).relative_to(home).parts[:1] for s in (".git", "marketplace", "venv")):
            for n in dns + fns:
                before[Path(dp) / n] = os.lstat(Path(dp) / n).st_mode
    for name in (".git", "marketplace", "venv"):
        before[home / name] = os.lstat(home / name).st_mode
    _perms().tighten_data_home(home)
    for p, m in before.items():
        assert os.lstat(p).st_mode == m, p
    assert stat.S_IMODE(os.stat(outdir / "t.py").st_mode) == 0o644


def test_c3_not_owned_paths_skipped_and_listed(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _loose_tree(home)
    mod = _perms()
    monkeypatch.setattr(os, "getuid", lambda: 424242)
    res = mod.tighten_data_home(home)
    assert res.changed == []
    assert res.errors, "foreign-owned paths must be listed in errors"
    assert stat.S_IMODE(os.stat(home / "knowledge.db").st_mode) == 0o644


def test_c3_never_raises_on_missing_home(tmp_path):
    res = _perms().tighten_data_home(tmp_path / "does-not-exist")
    assert res.changed == []


# ---- POSIX ACL cells (#4445 fold 4; ruled by the security seat on the issue) -----------------
# Contract: on a path with an access ACL xattr, ONLY the GROUP_OBJ and OTHER entries go to 0;
# USER_OBJ, named entries and the MASK are untouched; nothing calls chmod (the group bits of
# the mode ARE the mask) and nothing calls setfacl. A directory's default ACL gets the same
# rewrite. A blob that does not parse is left alone and reported in errors.

U_OBJ, U, G_OBJ, G, MASK, OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
R, W, X = 4, 2, 1
ACCESS_XATTR, DEFAULT_XATTR = "system.posix_acl_access", "system.posix_acl_default"

# FIXTURES LIST: real blobs measured on a Linux seat (the security seat, 2026-11-08, from
# `getfattr -n system.posix_acl_access -e hex` before and after `setfacl -n -m g::---,o::---`,
# which leaves the mask byte-identical):
# (label, access/default xattr blob as hex, expected tightened blob as hex or None when
# already tight).
REAL_ACL_BLOB_FIXTURES: list[tuple[str, str, "str | None"]] = [
    ("file 640, u:nobody:rw, m::rw (access)",
     "0200000001000600ffffffff02000600feff000004000400ffffffff10000600ffffffff20000000ffffffff",
     "0200000001000600ffffffff02000600feff000004000000ffffffff10000600ffffffff20000000ffffffff"),
    ("dir 750, u:nobody:rw, m::rw (access, and the same bytes as its default ACL)",
     "0200000001000700ffffffff02000600feff000004000500ffffffff10000600ffffffff20000000ffffffff",
     "0200000001000700ffffffff02000600feff000004000000ffffffff10000600ffffffff20000000ffffffff"),
    ("dir after the real setfacl -n run: already tight",
     "0200000001000700ffffffff02000600feff000004000000ffffffff10000600ffffffff20000000ffffffff",
     None),
]


def _blob(entries, version=2):
    import struct
    out = struct.pack("<I", version)
    for tag, perm, eid in entries:
        out += struct.pack("<HHI", tag, perm, eid)
    return out


def _entries(blob):
    import struct
    return [struct.unpack_from("<HHI", blob, 4 + 8 * i) for i in range((len(blob) - 4) // 8)]


_OPEN = [(U_OBJ, R | W, 0xFFFFFFFF), (U, R | W, 1000), (G_OBJ, R, 0xFFFFFFFF),
         (MASK, R | W, 0xFFFFFFFF), (OTHER, R, 0xFFFFFFFF)]


def test_c3_acl_blob_group_and_other_zeroed_everything_else_identical():
    mod = _perms()
    blob = _blob(_OPEN)
    new = mod._tighten_acl_blob(blob)
    assert new is not None and len(new) == len(blob)
    before, after = _entries(blob), _entries(new)
    for b, a in zip(before, after):
        if b[0] in (G_OBJ, OTHER):
            assert a == (b[0], 0, b[2])
        else:
            assert a == b, f"entry {b} must be untouched"


def test_c3_acl_blob_order_preserved_and_named_group_entries_untouched():
    mod = _perms()
    shuffled = [(OTHER, R, 0xFFFFFFFF), (G, R | X, 50), (MASK, R | X, 0xFFFFFFFF),
                (G_OBJ, R | X, 0xFFFFFFFF), (U, R, 1000), (U_OBJ, R | W | X, 0xFFFFFFFF)]
    new = mod._tighten_acl_blob(_blob(shuffled))
    got = _entries(new)
    assert [e[0] for e in got] == [e[0] for e in shuffled]
    assert dict(((t, i), p) for t, p, i in got)[(G, 50)] == R | X
    assert dict(((t, i), p) for t, p, i in got)[(MASK, 0xFFFFFFFF)] == R | X


def test_c3_acl_blob_already_tight_returns_none():
    mod = _perms()
    tight = _blob([(U_OBJ, R | W, 0xFFFFFFFF), (U, R, 1000), (G_OBJ, 0, 0xFFFFFFFF),
                   (MASK, R, 0xFFFFFFFF), (OTHER, 0, 0xFFFFFFFF)])
    assert mod._tighten_acl_blob(tight) is None
    assert mod._tighten_acl_blob(mod._tighten_acl_blob(_blob(_OPEN))) is None


_MALFORMED = {
    "wrong_version": _blob(_OPEN, version=1),
    "truncated": _blob(_OPEN)[:-3],
    "too_short": b"\x02\x00",
    "no_group_obj": _blob([e for e in _OPEN if e[0] != G_OBJ]),
    "no_other": _blob([e for e in _OPEN if e[0] != OTHER]),
    "duplicate_group_obj": _blob(_OPEN + [(G_OBJ, R, 0xFFFFFFFF)]),
}


@pytest.mark.parametrize("name", sorted(_MALFORMED))
def test_c3_acl_malformed_blob_is_none_and_never_written(tmp_path, monkeypatch, name):
    mod = _perms()
    assert mod._tighten_acl_blob(_MALFORMED[name]) is None
    home = tmp_path / "h"
    _loose_tree(home)
    target = str(home / "health-snapshots")
    monkeypatch.setattr(os, "getxattr",
                        lambda p, n, **k: _MALFORMED[name] if (str(p) == target and n == ACCESS_XATTR)
                        else (_ for _ in ()).throw(OSError(_errno.ENODATA, "no data")), raising=False)

    def boom(*a, **k):
        raise AssertionError("setxattr must not be called for a malformed blob")
    monkeypatch.setattr(os, "setxattr", boom, raising=False)
    res = mod.tighten_data_home(home)
    assert any("health-snapshots" in e for e in res.errors), res.errors
    assert "health-snapshots" not in res.changed
    assert stat.S_IMODE(os.stat(home / "health-snapshots").st_mode) == 0o755, "left alone, not chmod'ed"


def _fake_xattrs(monkeypatch, store):
    """store: {(path, name): blob}. Records setxattr calls."""
    calls = []

    def getx(p, n, **k):
        try:
            return store[(str(p), n)]
        except KeyError:
            raise OSError(_errno.ENODATA, "no data")

    def setx(p, n, v, *a, **k):
        calls.append((str(p), n))
        store[(str(p), n)] = bytes(v)
    monkeypatch.setattr(os, "getxattr", getx, raising=False)
    monkeypatch.setattr(os, "setxattr", setx, raising=False)
    return calls


def test_c3_acl_dir_with_access_and_default_both_rewritten_never_chmoded(tmp_path, monkeypatch):
    mod = _perms()
    home = tmp_path / "h"
    _loose_tree(home)
    d = str(home / "health-snapshots")
    store = {(d, ACCESS_XATTR): _blob(_OPEN), (d, DEFAULT_XATTR): _blob(_OPEN)}
    calls = _fake_xattrs(monkeypatch, store)
    chmods = []
    real = os.chmod
    monkeypatch.setattr(os, "chmod", lambda p, *a, **k: (chmods.append(str(p)), real(p, *a, **k))[1])
    res = mod.tighten_data_home(home)
    assert res.errors == []
    assert d not in chmods, "an ACL path is never chmod'ed (its group bits are the mask)"
    assert "health-snapshots (both)" in res.acl_preserved
    for name in (ACCESS_XATTR, DEFAULT_XATTR):
        got = _entries(store[(d, name)])
        assert [e for e in got if e[0] in (G_OBJ, OTHER)] == [(G_OBJ, 0, 0xFFFFFFFF), (OTHER, 0, 0xFFFFFFFF)]
        assert [e for e in got if e[0] in (U, MASK)] == [e for e in _OPEN if e[0] in (U, MASK)]
    assert sorted(calls) == sorted([(d, ACCESS_XATTR), (d, DEFAULT_XATTR)])
    # idempotent: nothing left to rewrite
    calls.clear()
    again = mod.tighten_data_home(home)
    assert calls == [] and "health-snapshots" not in again.changed
    assert "health-snapshots (both)" in again.acl_preserved


def test_c3_acl_access_only_and_default_only_are_distinguishable(tmp_path, monkeypatch):
    mod = _perms()
    home = tmp_path / "h"
    _loose_tree(home)
    a, b = str(home / "diary"), str(home / "sessions")
    store = {(a, ACCESS_XATTR): _blob(_OPEN), (b, DEFAULT_XATTR): _blob(_OPEN)}
    _fake_xattrs(monkeypatch, store)
    res = mod.tighten_data_home(home)
    assert "diary (access)" in res.acl_preserved
    assert "sessions (default)" in res.acl_preserved
    assert stat.S_IMODE(os.stat(home / "sessions").st_mode) == 0o700, "default-only dir is chmod'ed"


def test_c3_acl_branch_skipped_where_getxattr_is_absent(tmp_path, monkeypatch):
    mod = _perms()
    monkeypatch.delattr(os, "getxattr", raising=False)

    def boom(*a, **k):
        raise AssertionError("no xattr call without getxattr")
    monkeypatch.setattr(os, "setxattr", boom, raising=False)
    home = tmp_path / "h"
    _loose_tree(home)
    res = mod.tighten_data_home(home)
    assert res.errors == [] and res.acl_preserved == []
    _assert_tight(home)


def _getfacl_text(path) -> str:
    """`getfacl -p <path>`: BOTH ACL sets in one output, the default entries prefixed `default:`.
    (`getfacl -d` prints the default set UNPREFIXED, which is what made the first version of
    the Linux cell unable to match anything; do not add -d.)"""
    return subprocess.run(["getfacl", "-p", str(path)], capture_output=True, text=True,
                          check=True).stdout


def _parse_getfacl(text: str) -> dict:
    """Pure: getfacl -p text -> {"access": {key: (perm, effective)}, "default": {...}} where key
    is `tag:qualifier` (`user:nobody`, `group::`, `mask::`, `other::`, `user::`). The effective
    permission is the `#effective:` column when getfacl prints one (only when the mask limits
    the entry), else the entry's own permission. Comments and blank lines are skipped. A set
    with no matching lines is simply empty; callers must go through _entry(), which refuses an
    empty selection."""
    out = {"access": {}, "default": {}}
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        which = "access"
        if ln.startswith("default:"):
            which, ln = "default", ln[len("default:"):]
        body, _, eff = ln.partition("#effective:")
        fields = body.strip().split(":")
        if len(fields) != 3:
            continue
        perm = fields[2].split()[0] if fields[2].split() else ""
        key = f"{fields[0]}:{fields[1]}" if fields[1] else f"{fields[0]}::"
        out[which][key] = (perm, eff.strip() or perm)
    return out


def _entry(parsed: dict, which: str, key: str):
    """The (perm, effective) of one entry; an AssertionError when it is not there, so a cell
    reading from an empty selection fails loudly instead of comparing [] == []."""
    got = parsed[which].get(key)
    assert got is not None, (
        f"selection is empty: no {key!r} in the {which} ACL (the cell would be vacuous); "
        f"saw {sorted(parsed[which])}"
    )
    return got


# FIXTURES LIST (to be filled by the coordinator with real `getfacl -p` output texts measured on a
# Linux seat): (label, text). Every text must carry user:nobody and mask:: in the access set, and
# in the default set when it has any `default:` line.
REAL_GETFACL_FIXTURES: list[tuple[str, str]] = []

_SYNTH_BOTH = """# file: d
# owner: u
# group: g
user::rwx
user:nobody:rw-\t\t#effective:r--
group::---
mask::r--
other::---
default:user::rwx
default:user:nobody:r-x
default:group::---
default:mask::r-x
default:other::---

"""
_SYNTH_ACCESS_ONLY = "# file: f\nuser::rw-\nuser:nobody:rw-\ngroup::---\nmask::rw-\nother::---\n"
_SYNTH_UNPREFIXED_DEFAULT = "# file: d\nuser::rwx\nuser:nobody:r-x\ngroup::---\nmask::r-x\nother::---\n"


def test_getfacl_parse_keeps_access_and_default_sets_apart():
    parsed = _parse_getfacl(_SYNTH_BOTH)
    assert _entry(parsed, "access", "user:nobody") == ("rw-", "r--")   # effective column wins
    assert _entry(parsed, "default", "user:nobody") == ("r-x", "r-x")  # not confusable with access
    assert _entry(parsed, "access", "mask::") == ("r--", "r--")
    assert _entry(parsed, "default", "mask::") == ("r-x", "r-x")
    assert _entry(parsed, "access", "group::")[1] == "---"
    assert _entry(parsed, "default", "other::")[1] == "---"


def test_getfacl_empty_selection_is_refused_not_vacuous():
    """No default lines in the text: the default selection is empty and reading it FAILS."""
    parsed = _parse_getfacl(_SYNTH_ACCESS_ONLY)
    assert parsed["default"] == {}
    with pytest.raises(AssertionError, match="selection is empty"):
        _entry(parsed, "default", "mask::")
    # the `getfacl -d` shape (default set printed unprefixed) lands in the ACCESS set, so a
    # reader asking for the default set gets an empty selection and fails, never [] == [].
    unprefixed = _parse_getfacl(_SYNTH_UNPREFIXED_DEFAULT)
    assert unprefixed["default"] == {}
    with pytest.raises(AssertionError, match="selection is empty"):
        _entry(unprefixed, "default", "user:nobody")
    with pytest.raises(AssertionError, match="selection is empty"):
        _entry(_parse_getfacl(""), "access", "mask::")


@pytest.mark.parametrize("label,text", REAL_GETFACL_FIXTURES)
def test_getfacl_parse_real_fixture_texts(label, text):
    parsed = _parse_getfacl(text)
    _entry(parsed, "access", "user:nobody")
    _entry(parsed, "access", "mask::")
    if "default:" in text:
        _entry(parsed, "default", "user:nobody")
        _entry(parsed, "default", "mask::")


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("setfacl") is None,
                    reason="POSIX ACL cell: Linux with setfacl only (does not run on macOS)")
def test_c3_acl_effective_permissions_linux(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    f = home / "diary" / "f.txt"
    d = home / "history"
    # Discriminating fixture: the access entry (rw-) and the default entry (r-x) differ, so the
    # two sets cannot be confused.
    subprocess.run(["setfacl", "-m", "u:nobody:rw,m::rw", str(f)], check=True)
    subprocess.run(["setfacl", "-m", "u:nobody:rw,m::rw", str(d)], check=True)
    subprocess.run(["setfacl", "-d", "-m", "u:nobody:rx,m::rx", str(d)], check=True)
    pre_f, pre_d = _parse_getfacl(_getfacl_text(f)), _parse_getfacl(_getfacl_text(d))
    # every selection must be non-empty BEFORE anything is compared
    masks = {
        "f": _entry(pre_f, "access", "mask::"),
        "d": _entry(pre_d, "access", "mask::"),
        "dd": _entry(pre_d, "default", "mask::"),
    }
    assert _entry(pre_f, "access", "user:nobody")[0] == "rw-"
    assert _entry(pre_d, "access", "user:nobody")[0] == "rw-"
    assert _entry(pre_d, "default", "user:nobody")[0] == "r-x"
    res = _perms().tighten_data_home(home)
    assert res.errors == [], res.errors
    assert "diary/f.txt (access)" in res.acl_preserved
    assert "history (both)" in res.acl_preserved
    post_f, post_d = _parse_getfacl(_getfacl_text(f)), _parse_getfacl(_getfacl_text(d))
    for parsed, which, want in ((post_f, "access", "rw-"), (post_d, "access", "rw-"),
                                (post_d, "default", "r-x")):
        assert _entry(parsed, which, "user:nobody")[1] == want, (which, parsed)
        assert _entry(parsed, which, "group::")[1] == "---", (which, parsed)
        assert _entry(parsed, which, "other::")[1] == "---", (which, parsed)
    assert _entry(post_f, "access", "mask::") == masks["f"]
    assert _entry(post_d, "access", "mask::") == masks["d"]
    assert _entry(post_d, "default", "mask::") == masks["dd"]
    again = _perms().tighten_data_home(home)
    assert again.changed == []


@pytest.mark.parametrize("label,blob_hex,expect_hex", REAL_ACL_BLOB_FIXTURES)
def test_c3_acl_real_blob_fixtures(label, blob_hex, expect_hex):
    new = _perms()._tighten_acl_blob(bytes.fromhex(blob_hex))
    assert (new.hex() if new is not None else None) == expect_hex, label


# ---- fold 5 (reviewer round 1) ---------------------------------------------------------------
def _acl_getx(monkeypatch, target, err):
    """getxattr raising `err` for the access ACL of `target`, ENODATA for everything else."""
    def getx(p, n, **k):
        raise OSError(err if (str(p) == target and n == ACCESS_XATTR) else _errno.ENODATA, "x")
    monkeypatch.setattr(os, "getxattr", getx, raising=False)


def test_c3_getxattr_hard_error_is_reported_and_path_not_chmoded(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _loose_tree(home)
    target = str(home / "health-snapshots")
    _acl_getx(monkeypatch, target, _errno.EIO)
    res = _perms().tighten_data_home(home)
    assert any(e.startswith("health-snapshots:") for e in res.errors), res.errors
    assert "health-snapshots" not in res.changed
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755, "unread ACL: never chmod"
    assert not any(a.startswith("health-snapshots") for a in res.acl_preserved)


@pytest.mark.parametrize("err", [e for e in (getattr(_errno, n, None)
                                              for n in ("ENODATA", "ENOTSUP", "EOPNOTSUPP", "ENOSYS")) if e])
def test_c3_getxattr_no_acl_errnos_mean_no_acl(tmp_path, monkeypatch, err):
    home = tmp_path / "h"
    _loose_tree(home)
    target = str(home / "health-snapshots")
    _acl_getx(monkeypatch, target, err)
    res = _perms().tighten_data_home(home)
    assert res.errors == []
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700


def test_c3_setxattr_failure_is_an_error_not_acl_preserved_no_chmod_no_raise(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _loose_tree(home)
    d = str(home / "health-snapshots")
    store = {(d, ACCESS_XATTR): _blob(_OPEN)}
    _fake_xattrs(monkeypatch, store)

    def failing(*a, **k):
        raise OSError(_errno.EOPNOTSUPP, "not supported")
    monkeypatch.setattr(os, "setxattr", failing, raising=False)
    res = _perms().tighten_data_home(home)
    assert any(e.startswith("health-snapshots:") for e in res.errors), res.errors
    assert not any(a.startswith("health-snapshots") for a in res.acl_preserved)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o755


def test_c3_malformed_blob_is_not_acl_preserved(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _loose_tree(home)
    d = str(home / "health-snapshots")
    _fake_xattrs(monkeypatch, {(d, ACCESS_XATTR): _MALFORMED["truncated"]})
    res = _perms().tighten_data_home(home)
    assert any(e.startswith("health-snapshots:") for e in res.errors)
    assert not any(a.startswith("health-snapshots") for a in res.acl_preserved)


def test_c3_setxattr_does_not_follow_symlinks(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _loose_tree(home)
    d = str(home / "health-snapshots")
    seen = []
    store = {(d, ACCESS_XATTR): _blob(_OPEN)}
    _fake_xattrs(monkeypatch, store)
    inner = os.setxattr
    monkeypatch.setattr(os, "setxattr", lambda p, n, v, *a, **k: (seen.append(k), inner(p, n, v))[1],
                        raising=False)
    _perms().tighten_data_home(home)
    assert seen and all(k.get("follow_symlinks") is False for k in seen), seen


def test_c3_directory_is_never_widened(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    ro = home / "diary" / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        res = _perms().tighten_data_home(home)
        assert "diary/ro" not in res.changed
        assert stat.S_IMODE(os.stat(ro).st_mode) == 0o500
        # and a group/other-open directory still loses exactly those bits
        assert stat.S_IMODE(os.stat(home / "diary").st_mode) == 0o700
    finally:
        os.chmod(ro, 0o700)


def test_c3_hard_linked_file_is_skipped_and_outside_mode_kept(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    os.chmod(outside, 0o644)
    os.link(outside, home / "diary" / "hl.txt")
    res = _perms().tighten_data_home(home)
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o644
    assert "diary/hl.txt" not in res.changed
    assert any("diary/hl.txt" in e and "hard-linked, skipped" in e for e in res.errors), res.errors


def test_c6_merge_keeps_non_ascii_literal_utf8(tmp_path):
    raw = b'{\n  "name": "caf\\u00e9 \\u65e5\\u672c"\n}\n'  # JSON \\u escapes, ASCII bytes
    p = _seed_settings(tmp_path, raw)
    r = _run(tmp_path, ["--retention-days", "90"])
    assert r.returncode == 0, r.stdout + r.stderr
    data = p.read_bytes()
    assert "café 日本".encode("utf-8") in data and b"\\u00e9" not in data
    assert json.loads(data.decode("utf-8"))["cleanupPeriodDays"] == 90


def test_c6_final_write_oserror_is_refused_with_install_otherwise_complete(tmp_path):
    """The settings.json write is the guard's in-place rewrite; fail it part-way. The guard puts
    the old bytes back in the same file (its mtime moves: it was written twice) and says so; the
    step adds that the rest of the install is complete."""
    p = _seed_settings(tmp_path, b'{"model": "x"}\n')
    before_bytes, before_mode, before_ino = p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_ino
    code = (
        "import sys, importlib.util\n"
        f"sys.argv = [{str(BOOTSTRAP)!r}, '--retention-days', '90']\n"
        f"spec = importlib.util.spec_from_file_location('bootstrap_4445w', {str(BOOTSTRAP)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        # The settings.json write happens inside the guard's merge, in place; fail its first
        # write part-way (half the bytes, then a full disk). The put-back that follows succeeds.
        "import os\n"
        "real = m.fg._write_all\n"
        "calls = []\n"
        "def boom(fd, data):\n"
        "    calls.append(1)\n"
        "    if len(calls) == 1:\n"
        "        os.write(fd, data[: len(data) // 2]); raise OSError(28, 'No space left on device')\n"
        "    return real(fd, data)\n"
        "m.fg._write_all = boom\n"
        "m.main()\n"
    )
    env = os.environ.copy()
    env.update(_load_bootstrap(tmp_path))
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "REFUSED" in out and "otherwise complete" in out and "No space left" in out and "put back" in out
    assert p.read_bytes() == before_bytes and stat.S_IMODE(p.stat().st_mode) == before_mode
    assert p.stat().st_ino == before_ino
    assert (tmp_path / ".engram" / "knowledge.db").exists()


@pytest.mark.parametrize("bad", [" 30", "+30", "3_0", "\u0663\u0660", "30.0", "30 ", ""])
def test_c6_strict_retention_parse(tmp_path, bad):
    r = _run(tmp_path, ["--retention-days", bad])
    assert r.returncode != 0
    assert "--retention-days" in r.stdout + r.stderr
    assert not _settings(tmp_path).exists()


# ---- fold 6 (reviewer round 2) ---------------------------------------------------------------

@pytest.mark.parametrize("err", [_errno.EINVAL, _errno.EPERM, _errno.EACCES])
def test_c3_getxattr_other_errnos_still_fail_closed(tmp_path, monkeypatch, err):
    home = tmp_path / "h"
    _loose_tree(home)
    target = str(home / "health-snapshots")
    _acl_getx(monkeypatch, target, err)
    res = _perms().tighten_data_home(home)
    assert any(e.startswith("health-snapshots:") for e in res.errors), res.errors
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755


# 0o750 / 0o705 / 0o775 / 0o757 cannot tell `cur & ~0o077` from `min(cur, 0o700)` (both give
# 0o700); 0o550 and 0o505 can (owner bits below rwx: the right answer keeps them, 0o500).
@pytest.mark.parametrize("mode,want", [(0o750, 0o700), (0o705, 0o700), (0o775, 0o700),
                                       (0o757, 0o700), (0o550, 0o500), (0o505, 0o500)])
def test_c3_directory_loses_exactly_group_and_other_bits(tmp_path, mode, want):
    home = tmp_path / "h"
    _loose_tree(home)
    d = home / "diary" / "sub750"
    d.mkdir()
    os.chmod(d, mode)
    try:
        _perms().tighten_data_home(home)
        assert stat.S_IMODE(os.stat(d).st_mode) == want
    finally:
        os.chmod(d, 0o700)


def test_c6_lone_surrogate_in_settings_is_refused_not_a_traceback(tmp_path):
    """Content that cannot be encoded is refused before the file is opened or backed up: bytes,
    mode, mtime and inode all unchanged, and no stamped backup."""
    p = _seed_settings(tmp_path, b'{"note": "\\ud800"}\n')
    before, before_ino = _snap(p), p.stat().st_ino
    r = _run(tmp_path, ["--retention-days", "90"])
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "Traceback" not in out, out
    assert "REFUSED" in out and "otherwise complete" in out
    assert _snap(p) == before and p.stat().st_ino == before_ino
    assert list(p.parent.glob("settings.json.engram-backup.*")) == []
    assert (tmp_path / ".engram" / "knowledge.db").exists()


def _hardlinked_tight_home(tmp_path):
    home = tmp_path / "h"
    _loose_tree(home)
    _perms().tighten_data_home(home)
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    os.chmod(outside, 0o644)
    os.link(outside, home / "diary" / "hl.txt")
    return home


def test_c3_hook_errors_only_prints_one_bounded_count_line(tmp_path, monkeypatch, capsys):
    home = _hardlinked_tight_home(tmp_path)
    mod = _load_hook(monkeypatch, home, tmp_path)
    capsys.readouterr()
    mod._tighten_engram_home()
    out = capsys.readouterr()
    assert out.out == ""
    lines = [ln for ln in out.err.splitlines() if ln.strip()]
    assert lines == ["[engram] data home permissions: 1 path(s) could not be tightened"], out.err
    assert "owner" not in out.err.lower() and "hl.txt" not in out.err


def test_c3_hook_changed_and_errors_is_one_line_covering_both(tmp_path, monkeypatch, capsys):
    home = _hardlinked_tight_home(tmp_path)
    old = os.umask(0)
    try:
        (home / "sessions" / "new.json").write_text("{}")
        os.chmod(home / "sessions" / "new.json", 0o666)
    finally:
        os.umask(old)
    mod = _load_hook(monkeypatch, home, tmp_path)
    capsys.readouterr()
    mod._tighten_engram_home()
    out = capsys.readouterr()
    assert out.out == ""
    lines = [ln for ln in out.err.splitlines() if ln.strip()]
    assert len(lines) == 1, out.err
    assert "1 path(s) tightened" in lines[0] and "1 could not be tightened" in lines[0]
    assert "owner" not in lines[0].lower()


# ---- fold 7: cells restored after an accidental deletion (commit 44602aca), non-path homes ----

def test_c3_symlinked_home_root_is_resolved_and_tightened(tmp_path):
    real = tmp_path / "real"
    _loose_tree(real)
    link = tmp_path / "link"
    os.symlink(real, link)
    res = _perms().tighten_data_home(link)
    assert res.errors == []
    assert "." in res.changed and "knowledge.db" in res.changed
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(real / "knowledge.db").st_mode) == 0o600
    assert os.path.islink(link)
    _assert_tight(real)


def test_c3_dangling_home_symlink_is_an_error_entry_not_a_raise(tmp_path):
    link = tmp_path / "dangling"
    os.symlink(tmp_path / "nowhere", link)
    res = _perms().tighten_data_home(link)
    assert res.changed == [] and res.errors
    # an unresolved root would only say "not a directory"; resolving reports that nothing is there
    assert any("does not resolve to anything" in e for e in res.errors), res.errors


def test_c3_home_symlink_to_a_file_is_an_error_entry(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    os.chmod(f, 0o644)
    link = tmp_path / "tofile"
    os.symlink(f, link)
    res = _perms().tighten_data_home(link)
    assert res.changed == [] and any("not a directory" in e for e in res.errors)
    # the message names the RESOLVED target, which an unresolved root would not
    assert any(os.path.realpath(f) in e for e in res.errors), res.errors
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o644


class _BytesPath:
    """An os.PathLike whose __fspath__ returns bytes (os.fspath accepts it)."""

    def __init__(self, p):
        self._p = os.fsencode(str(p))

    def __fspath__(self):
        return self._p


def _snapshot_all(root: Path):
    """Mode of EVERY path under root (not only the tighten scope), so a stray chmod shows."""
    out = {root: os.lstat(root).st_mode}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            q = Path(dp) / n
            out[q] = os.lstat(q).st_mode
    return out


@pytest.mark.parametrize(
    "bad",
    [None, 0, 42, "", Path(""), Path(), Path("."), ".", "rel/home", "./rel/home", Path("rel/home")],
    ids=["none", "zero", "int", "empty-string", "path-empty", "path-bare", "path-dot", "dot-string",
         "relative-string", "dot-relative-string", "relative-path"],
)
def test_c3_non_path_home_is_an_error_entry_and_changes_nothing(tmp_path, monkeypatch, bad):
    """None and 42 are not paths; "" / Path("") / "." / a relative path would resolve against the
    CURRENT DIRECTORY, which must never be tightened by accident (#4479). cwd is a scratch loose
    tree (with a loose tree at rel/home too, so a relative home that DID resolve would be
    visible) so a wrong answer is visible and a failure cannot touch the repo."""
    cwd = tmp_path / "cwd"
    _loose_tree(cwd)
    (cwd / "rel").mkdir()
    _loose_tree(cwd / "rel" / "home")
    monkeypatch.chdir(cwd)
    before = _snapshot_all(cwd)
    res = _perms().tighten_data_home(bad)
    assert res.changed == [] and len(res.errors) == 1, res
    assert "is not a usable path, nothing was changed" in res.errors[0], res.errors
    assert _snapshot_all(cwd) == before


@pytest.mark.parametrize("kind", ["bytes", "pathlike-bytes"])
def test_c3_bytes_home_is_refused_and_the_whole_tree_is_unchanged(tmp_path, monkeypatch, kind):
    """A bytes home (or a PathLike whose __fspath__ returns bytes) is REFUSED, not half-handled:
    on the unfixed tree the root was tightened but diary/ and diary/f.txt were left loose
    (#4479). The real loose tree is absolute, so only the type can be what refuses it."""
    home = tmp_path / "h"
    _loose_tree(home)
    monkeypatch.chdir(tmp_path)  # scratch cwd, never the repo
    before = _snapshot_all(home)
    arg = os.fsencode(str(home)) if kind == "bytes" else _BytesPath(home)
    res = _perms().tighten_data_home(arg)
    assert res.changed == [] and len(res.errors) == 1, res
    assert "is not a usable path, nothing was changed" in res.errors[0], res.errors
    assert _snapshot_all(home) == before
    assert stat.S_IMODE(os.lstat(home).st_mode) == 0o755
    assert stat.S_IMODE(os.lstat(home / "diary").st_mode) == 0o755
    assert stat.S_IMODE(os.lstat(home / "diary" / "f.txt").st_mode) == 0o644


def test_c3_absolute_str_and_path_homes_still_tighten(tmp_path):
    """The refusal must not catch the forms that work: an absolute str and an absolute Path."""
    for i, wrap in enumerate((str, Path)):
        home = tmp_path / f"h{i}"
        _loose_tree(home)
        res = _perms().tighten_data_home(wrap(home))
        assert res.errors == [], res
        _assert_tight(home)


# The contract cells this file must keep. Deleting one (it has happened: three cells vanished
# in an unrelated commit) reddens this cell; renaming one means updating this list on purpose.
REQUIRED_CONTRACT_CELLS = (
    "test_c1a_no_flag_never_touches_settings",
    "test_c1b_flag_creates_absent_file_mode_0600",
    "test_c1c_bad_values_refused_naming_flag_and_range",
    "test_c1_codex_target_says_retention_flag_is_ignored",
    "test_c2_non_object_settings_backed_up_and_refused",
    "test_c2_preflight_refuses_before_anything_is_written",
    "test_c2_repair_then_rerun_with_same_flag_succeeds",
    "test_c2_ordering_tighten_ran_even_though_final_step_refused",
    "test_c3_fresh_bootstrap_home_is_owner_only",
    "test_c3_data_dirs_set_is_pinned",
    "test_c3_symlinked_home_root_is_resolved_and_tightened",
    "test_c3_dangling_home_symlink_is_an_error_entry_not_a_raise",
    "test_c3_home_symlink_to_a_file_is_an_error_entry",
    "test_c3_non_path_home_is_an_error_entry_and_changes_nothing",
    "test_c3_bytes_home_is_refused_and_the_whole_tree_is_unchanged",
    "test_c3_absolute_str_and_path_homes_still_tighten",
    "test_c3_symlinks_never_followed_or_changed",
    "test_a_cell_makes_the_home_root_itself_a_symlink",
    "test_c3_hard_linked_file_is_skipped_and_outside_mode_kept",
    "test_c3_directory_is_never_widened",
    "test_c3_acl_blob_group_and_other_zeroed_everything_else_identical",
    "test_c3_acl_malformed_blob_is_none_and_never_written",
    "test_c3_acl_dir_with_access_and_default_both_rewritten_never_chmoded",
    "test_c3_acl_effective_permissions_linux",
    "test_getfacl_empty_selection_is_refused_not_vacuous",
    "test_c3_hook_tightens_with_one_stderr_line_and_empty_stdout",
    "test_c3_hook_errors_only_prints_one_bounded_count_line",
    "test_c3_hook_never_raises_when_module_missing",
    "test_c6_lone_surrogate_in_settings_is_refused_not_a_traceback",
    "test_c6_strict_retention_parse",
)


def test_a_cell_makes_the_home_root_itself_a_symlink():
    """Counting cells by name never drops (a name containing "symlink" survives anywhere); the
    PROPERTY that matters is that some cell hands tighten_data_home a path that IS a symlink.
    Structural scan of this file: inside one test function, `os.symlink(x, link)` followed by
    `tighten_data_home(link)` with the SAME name `link`. The three symlinked-home cells (resolved,
    dangling, to-a-file) must each satisfy it; deleting or rewriting one reddens this cell."""
    import ast
    tree = ast.parse(Path(__file__).read_text())
    found = set()
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        linked, tightened = set(), set()
        for c in ast.walk(fn):
            if not isinstance(c, ast.Call):
                continue
            f = c.func
            if isinstance(f, ast.Attribute) and f.attr == "symlink" and len(c.args) >= 2 \
                    and isinstance(c.args[1], ast.Name):
                linked.add(c.args[1].id)
            if isinstance(f, ast.Attribute) and f.attr == "tighten_data_home" and c.args \
                    and isinstance(c.args[0], ast.Name):
                tightened.add(c.args[0].id)
        if linked & tightened:
            found.add(fn.name)
    assert {
        "test_c3_symlinked_home_root_is_resolved_and_tightened",
        "test_c3_dangling_home_symlink_is_an_error_entry_not_a_raise",
        "test_c3_home_symlink_to_a_file_is_an_error_entry",
    } <= found, f"cells that make the HOME ROOT a symlink: {sorted(found)}"


def test_contract_cells_are_all_still_present():
    import ast
    tree = ast.parse(Path(__file__).read_text())
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    missing = [n for n in REQUIRED_CONTRACT_CELLS if n not in defined]
    assert not missing, f"contract cells deleted or renamed: {missing}"


# ------------------------------------------------------------- SessionStart hook

_HOOK_PATH = SRC / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook(monkeypatch, engram_home, login):
    from unittest.mock import MagicMock
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    monkeypatch.setenv("HOME", str(login))
    stub = MagicMock()
    stub.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub)
    sys.modules.pop("engram_session_start_hook_4445", None)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook_4445", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_c3_hook_tightens_with_one_stderr_line_and_empty_stdout(tmp_path, monkeypatch, capsys):
    home = tmp_path / "h"
    _loose_tree(home)
    mod = _load_hook(monkeypatch, home, tmp_path)
    capsys.readouterr()
    mod._tighten_engram_home()
    out = capsys.readouterr()
    assert out.out == ""
    assert len([ln for ln in out.err.splitlines() if ln.strip()]) == 1
    _assert_tight(home)
    mod._tighten_engram_home()
    again = capsys.readouterr()
    assert again.out == "" and again.err == "", "nothing changed, nothing printed"


def test_c3_hook_never_raises_when_module_missing(tmp_path, monkeypatch, capsys):
    """The module is absent from the runtime dir: the REAL import fails (no patched resolver
    that raises), the hook prints nothing on stdout and the home is left as it was."""
    home = tmp_path / "h"
    _loose_tree(home)
    empty = tmp_path / "empty-runtime"
    empty.mkdir()
    mod = _load_hook(monkeypatch, home, tmp_path)
    monkeypatch.setattr(mod, "resolve_runtime_dir", lambda *a, **k: str(empty))
    monkeypatch.setitem(sys.modules, "engram_home_perms", None)  # an import now raises ImportError
    before = {p: os.lstat(p).st_mode for p in _in_scope(home)}
    capsys.readouterr()
    mod._tighten_engram_home()
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert {p: os.lstat(p).st_mode for p in _in_scope(home)} == before


def test_c3_hook_main_impl_calls_the_tightener():
    import ast
    tree = ast.parse(_HOOK_PATH.read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_main_impl")
    calls = [c.func.id for c in ast.walk(main) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert "_tighten_engram_home" in calls
