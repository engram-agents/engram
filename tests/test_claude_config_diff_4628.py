"""#4628 ask 1: tools/claude_config_diff.py and its wiring into install-local-marketplace.sh.

The contract (C1..C8) is stated in the module docstring of the helper. Written before the
helper existed; every cell below pins one rule, and each rule has a plausible-wrong
implementation that turns exactly its own cells red:

  C1  one line per added / removed / changed dotted key path; key order and whitespace are not a change
  C2  a string value is never printed; bool / number print `old -> new`; other types `changed (<type>)`
  C3  file created / removed / unparseable (sha256 compare)
  C4  a backup only when something would be lost (removed or changed), never for additions alone
  C5  never fails the install: exit 0 always, one `claude-config diff unavailable: <Class>` line
  C6  only READS the Claude files
  C7  installer: stub `claude` drops a key; the run names it, prints the backup, exit code unchanged
  C8  --skip-add / no `claude` on PATH: no snapshot, no report
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "tools" / "claude_config_diff.py"
INSTALLER = REPO_ROOT / "tools" / "install-local-marketplace.sh"
TAG = "[install-local-marketplace] "
SECRET = "sk-ant-SECRETVALUE-9f3a"


def _run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(HELPER), *map(str, args)],
        capture_output=True, text=True, cwd=cwd,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _jw(path, obj, **kw):
    path.write_text(json.dumps(obj, **kw), encoding="utf-8")


class Rig:
    """One before/after run of the helper over a set of named files."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "claude-home"
        self.home.mkdir()
        self.snap = tmp_path / "snap"
        self.backups = tmp_path / "backups"

    def f(self, name):
        return self.home / name

    def snapshot(self, *names):
        return _run("snapshot", "--dest", self.snap, *[self.f(n) for n in names])

    def report(self, *names):
        return _run("report", "--snapshot", self.snap, "--backup-root", self.backups,
                    *[self.f(n) for n in names])

    def backup_dirs(self):
        return sorted(self.backups.glob("pre-marketplace-add-*")) if self.backups.exists() else []


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _lines(res):
    return [ln for ln in res.stdout.splitlines() if ln.strip()]


# ---------------------------------------------------------------- C1

def test_c1_reordered_and_reformatted_file_prints_nothing(rig):
    _jw(rig.f("a.json"), {"x": 1, "y": {"p": True, "q": [1, {"k": 2, "j": 3}]}}, indent=2)
    rig.snapshot("a.json")
    # same content, reversed key order, compact, nested list-of-object order changed too
    _jw(rig.f("a.json"), {"y": {"q": [1, {"j": 3, "k": 2}], "p": True}, "x": 1})
    res = rig.report("a.json")
    assert res.returncode == 0
    assert res.stdout == ""
    assert rig.backup_dirs() == []


def test_c1_removed_nested_key_is_named_by_dotted_path(rig):
    _jw(rig.f("k.json"), {"engram-local": {"source": {"path": "/m"}, "autoUpdate": True}, "other": {}})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"engram-local": {"source": {"path": "/m"}}, "other": {}})
    res = rig.report("k.json")
    assert res.returncode == 0
    assert "engram-local.autoUpdate" in res.stdout
    assert "removed" in res.stdout
    assert "engram-local.source" not in res.stdout


def test_c1_added_key_is_named(rig):
    _jw(rig.f("k.json"), {"a": 1})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"a": 1, "b": {"c": 1}})
    out = rig.report("k.json").stdout
    assert "b" in out and "added" in out


def test_c1_list_is_compared_as_a_whole_value(rig):
    _jw(rig.f("k.json"), {"l": [1, 2, 3], "same": [1, 2]})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"l": [1, 2, 4], "same": [1, 2]})
    out = rig.report("k.json").stdout
    assert "l: changed (array)" in out
    assert "same" not in out
    assert "l.0" not in out and "l[" not in out


# ---------------------------------------------------------------- C2

def test_c2_bool_and_number_print_old_to_new(rig):
    _jw(rig.f("k.json"), {"flag": True, "n": 3})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"flag": False, "n": 4})
    out = rig.report("k.json").stdout
    assert "flag: true -> false" in out
    assert "n: 3 -> 4" in out


def test_c2_string_values_never_appear_changed_or_unchanged(rig):
    _jw(rig.f("k.json"), {"token": SECRET, "model": "opus-" + SECRET, "keep": {"apiKey": SECRET}})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"token": SECRET, "model": "sonnet-" + SECRET + "-NEW", "keep": {"apiKey": SECRET}})
    res = rig.report("k.json")
    assert "model: changed (string)" in res.stdout
    assert "SECRETVALUE" not in res.stdout
    assert "SECRETVALUE" not in res.stderr
    # the backup (a copy of the user's own file in the user's own home) is allowed to hold it,
    # but the path line must not carry it either
    assert "sonnet" not in res.stdout and "opus" not in res.stdout


def test_c2_type_change_names_both_types_no_values(rig):
    _jw(rig.f("k.json"), {"v": "text-" + SECRET, "w": {"a": 1}})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"v": 7, "w": "now-a-string-" + SECRET})
    out = rig.report("k.json").stdout
    assert "v: changed (string -> number)" in out
    assert "w: changed (object -> string)" in out
    assert "SECRETVALUE" not in out and " 7" not in out


# ---------------------------------------------------------------- C3

def test_c3_created_and_removed_files(rig):
    _jw(rig.f("gone.json"), {"a": 1})
    rig.snapshot("gone.json", "new.json")
    rig.f("gone.json").unlink()
    _jw(rig.f("new.json"), {"b": 2})
    res = rig.report("gone.json", "new.json")
    assert "created: new.json" in res.stdout
    assert "removed: gone.json" in res.stdout
    # a removed file holds what the user had: it is backed up
    (d,) = rig.backup_dirs()
    assert json.loads((d / "gone.json").read_text()) == {"a": 1}
    assert not (d / "new.json").exists()


def test_c3_absent_before_and_after_prints_nothing(rig):
    rig.snapshot("none.json")
    res = rig.report("none.json")
    assert res.returncode == 0 and res.stdout == ""


def test_c3_unparseable_changed_bytes_reported_without_content(rig):
    rig.f("s.json").write_text("not json " + SECRET)
    rig.snapshot("s.json")
    rig.f("s.json").write_text("still not json " + SECRET + "!")
    res = rig.report("s.json")
    assert "changed (not parsed as JSON): s.json" in res.stdout
    assert "SECRETVALUE" not in res.stdout
    assert len(rig.backup_dirs()) == 1


def test_c3_unparseable_identical_bytes_prints_nothing(rig):
    rig.f("s.json").write_text("not json")
    rig.snapshot("s.json")
    res = rig.report("s.json")
    assert res.stdout == "" and rig.backup_dirs() == []


def test_c3_json_that_is_not_an_object_counts_as_unparsed(rig):
    rig.f("s.json").write_text("[1, 2]")
    rig.snapshot("s.json")
    rig.f("s.json").write_text("[1, 3]")
    assert "changed (not parsed as JSON): s.json" in rig.report("s.json").stdout


# ---------------------------------------------------------------- C4

def test_c4_removed_or_changed_key_keeps_backup_with_modes_and_prints_path(rig):
    _jw(rig.f("k.json"), {"a": True, "b": "x"})
    before = rig.f("k.json").read_bytes()
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"a": False})
    res = rig.report("k.json")
    (d,) = rig.backup_dirs()
    assert d.name.startswith("pre-marketplace-add-")
    assert (d / "k.json").read_bytes() == before
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "k.json").stat().st_mode) == 0o600
    assert str(d / "k.json") in res.stdout


def test_c4_changed_only_also_keeps_backup(rig):
    _jw(rig.f("k.json"), {"a": True})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"a": False})
    rig.report("k.json")
    assert len(rig.backup_dirs()) == 1


def test_c4_added_keys_only_keeps_no_backup_and_announces_none(rig):
    _jw(rig.f("k.json"), {"a": 1})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"a": 1, "b": 2})
    res = rig.report("k.json")
    assert "added" in res.stdout
    assert rig.backup_dirs() == []
    assert "backup" not in res.stdout.lower()
    assert not rig.backups.exists() or list(rig.backups.iterdir()) == []


def test_c4_nothing_differs_keeps_no_backup(rig):
    _jw(rig.f("k.json"), {"a": 1})
    rig.snapshot("k.json")
    res = rig.report("k.json")
    assert res.stdout == "" and rig.backup_dirs() == []


# ---------------------------------------------------------------- output shape

def test_every_stdout_line_carries_the_installer_prefix(rig):
    _jw(rig.f("k.json"), {"a": 1, "b": 2})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"a": 2, "c": 2})
    res = rig.report("k.json")
    lines = res.stdout.splitlines()
    assert lines and all(ln.startswith(TAG) for ln in lines)
    assert res.stderr == ""


def test_a_file_that_did_not_change_is_not_named_when_another_did(rig):
    _jw(rig.f("one.json"), {"a": 1})
    _jw(rig.f("two.json"), {"a": 1})
    rig.snapshot("one.json", "two.json")
    _jw(rig.f("two.json"), {"a": 2})
    out = rig.report("one.json", "two.json").stdout
    assert "two.json" in out and "one.json" not in out


# ---------------------------------------------------------------- C5

def test_c5_snapshot_exits_zero_and_copies_bytes_0600_and_records_absent(rig):
    _jw(rig.f("k.json"), {"a": 1})
    res = rig.snapshot("k.json", "absent.json")
    assert res.returncode == 0 and res.stderr == ""
    copies = [p for p in rig.snap.rglob("*") if p.is_file() and p.read_bytes() == rig.f("k.json").read_bytes()]
    assert len(copies) == 1
    assert stat.S_IMODE(copies[0].stat().st_mode) == 0o600
    # a later creation of the absent file is seen as created, which proves the absence was recorded
    _jw(rig.f("absent.json"), {"z": 1})
    assert "created: absent.json" in rig.report("k.json", "absent.json").stdout


def test_c5_missing_snapshot_dir_says_so_and_exits_zero(rig):
    _jw(rig.f("k.json"), {"a": 1})
    res = rig.report("k.json")
    assert res.returncode == 0
    assert "no snapshot was taken" in res.stdout
    assert res.stderr == ""


def test_c5_exception_inside_report_prints_one_line_and_exits_zero(rig):
    _jw(rig.f("k.json"), {"a": 1})
    rig.snapshot("k.json")
    rig.f("k.json").unlink()
    rig.f("k.json").mkdir()  # a directory where the file was: reading it raises
    res = rig.report("k.json")
    assert res.returncode == 0
    assert _lines(res) == [TAG + "claude-config diff unavailable: IsADirectoryError"]
    assert res.stderr == ""


def test_c5_exception_inside_snapshot_prints_one_line_and_exits_zero(rig):
    rig.f("k.json").mkdir()
    res = rig.snapshot("k.json")
    assert res.returncode == 0
    assert _lines(res) == [TAG + "claude-config diff unavailable: IsADirectoryError"]


def test_c5_bad_arguments_still_exit_zero(rig):
    res = _run("report")
    assert res.returncode == 0
    assert "claude-config diff unavailable:" in res.stdout
    assert _run("bogus-subcommand").returncode == 0
    assert _run().returncode == 0


# ---------------------------------------------------------------- C6

def _tree(root):
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


def test_c6_report_reads_the_claude_files_and_writes_only_the_backup_root(rig, tmp_path):
    _jw(rig.f("k.json"), {"a": 1, "b": 2})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"a": 2})
    claude_before = _tree(rig.home)
    mtime_before = rig.f("k.json").stat().st_mtime_ns
    snap_before = _tree(rig.snap)
    rig.report("k.json")
    assert _tree(rig.home) == claude_before
    assert rig.f("k.json").stat().st_mtime_ns == mtime_before
    assert _tree(rig.snap) == snap_before
    assert {p.name for p in tmp_path.iterdir()} == {"claude-home", "snap", "backups"}


def test_c6_snapshot_does_not_touch_the_claude_files(rig):
    _jw(rig.f("k.json"), {"a": 1})
    before, mt = _tree(rig.home), rig.f("k.json").stat().st_mtime_ns
    rig.snapshot("k.json")
    assert _tree(rig.home) == before and rig.f("k.json").stat().st_mtime_ns == mt


def test_helper_source_reads_no_environment_and_names_no_home():
    text = HELPER.read_text()
    assert "os.environ" not in text and "getenv" not in text
    assert "~/.claude" not in text and "~/.engram" not in text


# ---------------------------------------------------------------- C7 / C8 (installer level)

def _login_home(tmp_path):
    home = tmp_path.parent / (tmp_path.name + "-login-home")
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture(autouse=True)
def _clean_login_home(tmp_path):
    import shutil
    shutil.rmtree(tmp_path.parent / (tmp_path.name + "-login-home"), ignore_errors=True)
    yield
    shutil.rmtree(tmp_path.parent / (tmp_path.name + "-login-home"), ignore_errors=True)


def _write_fake_plugin(build_root):
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "plugin.json").write_text(json.dumps({"name": "engram", "version": "0.1.0"}))
    (build_root / "platform.json").write_text(json.dumps({"platform": "claude-code"}))


STUB = r'''#!{python}
import json, os, sys
home = os.environ["CLAUDE_CONFIG_DIR"]
with open(os.path.join(home, "stub-claude-calls.log"), "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:4] == ["plugin", "marketplace", "add"]:
    kp = os.path.join(home, "plugins", "known_marketplaces.json")
    with open(kp) as fh:
        known = json.load(fh)
    known["engram-local"].pop("autoUpdate", None)
    with open(kp, "w") as fh:
        json.dump(known, fh, indent=2)
    sp = os.path.join(home, "settings.json")
    with open(sp) as fh:
        settings = json.load(fh)
    reordered = dict(reversed(list(settings.items())))
    tmp = sp + ".tmp-stub"
    with open(tmp, "w") as fh:
        json.dump(reordered, fh)
    os.replace(tmp, sp)
    sys.exit(int(os.environ.get("STUB_CLAUDE_EXIT", "0")))
'''


def _install(tmp_path, *, extra_args=(), with_claude=True, claude_exit=0, seed=True):
    build_root = tmp_path / "build" / "plugin"
    _write_fake_plugin(build_root)
    claude_home = tmp_path / "claude-config"
    (claude_home / "plugins").mkdir(parents=True)
    if seed:
        _jw(claude_home / "plugins" / "known_marketplaces.json",
            {"engram-local": {"source": {"source": "directory", "path": "/m"}, "autoUpdate": True},
             "elsewhere": {"autoUpdate": True}}, indent=2)
        _jw(claude_home / "settings.json",
            {"model": "opus-" + SECRET, "enabledPlugins": {"engram@engram-local": True}, "theme": "dark"},
            indent=2)
    stubs = tmp_path / "stubbin"
    stubs.mkdir()
    if with_claude:
        stub = stubs / "claude"
        stub.write_text(STUB.format(python=sys.executable))
        stub.chmod(0o755)
    # a PATH with no `claude` anywhere: every other executable symlinked into a private dir
    path = os.environ.get("PATH", "")
    if not with_claude:
        masked = tmp_path / "maskedbin"
        masked.mkdir()
        for entry in path.split(os.pathsep):
            try:
                names = os.listdir(entry)
            except OSError:
                continue
            for n in names:
                src = os.path.join(entry, n)
                if n == "claude" or (masked / n).exists() or not os.path.isfile(src) or not os.access(src, os.X_OK):
                    continue
                try:
                    os.symlink(src, masked / n)
                except OSError:
                    pass
        py = masked / "python3"
        if not py.exists():
            py.symlink_to(sys.executable)
        path = str(masked)
    else:
        path = str(stubs) + os.pathsep + path
    env = {**os.environ, "ENGRAM_HOME": str(tmp_path), "HOME": str(_login_home(tmp_path)),
           "CLAUDE_CONFIG_DIR": str(claude_home), "PATH": path, "STUB_CLAUDE_EXIT": str(claude_exit)}
    real_gitconfig = Path(os.environ.get("HOME", "")) / ".gitconfig"
    if real_gitconfig.is_file() and "GIT_CONFIG_GLOBAL" not in env:
        env["GIT_CONFIG_GLOBAL"] = str(real_gitconfig)
    (tmp_path / "tmpdir").mkdir()
    env["TMPDIR"] = str(tmp_path / "tmpdir")  # so a leftover snapshot folder is visible to the test
    env["ENGRAM_BUILD_DIR"] = str(build_root)
    # the venv step must find a python under the temp login home, never the real one
    venv_bin = _login_home(tmp_path) / ".engram" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake = venv_bin / "python"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake.chmod(0o755)
    res = subprocess.run(
        ["bash", str(INSTALLER), "--target", "claude-code", "--skip-build",
         "--allow-branch", "--allow-untagged", *extra_args],
        cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return res, claude_home


def test_c7_installer_names_the_removed_key_prints_the_backup_and_keeps_exit_code(tmp_path):
    res, claude_home = _install(tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "engram-local.autoUpdate" in res.stdout
    # settings.json was rewritten by rename with the same content in another key order: silence
    assert "settings.json" not in res.stdout
    assert "SECRETVALUE" not in res.stdout + res.stderr
    backups = sorted((tmp_path / "install-backups").glob("pre-marketplace-add-*"))
    assert len(backups) == 1
    assert str(backups[0] / "known_marketplaces.json") in res.stdout
    assert json.loads((backups[0] / "known_marketplaces.json").read_text())["engram-local"]["autoUpdate"] is True
    assert not (backups[0] / "settings.json").exists()
    # the report comes after the add and before the closing next-steps block
    assert res.stdout.index("Registering marketplace with Claude Code") < res.stdout.index("engram-local.autoUpdate")
    assert res.stdout.index("engram-local.autoUpdate") < res.stdout.index("Marketplace registered. Next steps")
    # the snapshot dir is gone
    assert (tmp_path / "tmpdir").is_dir()
    assert not list((tmp_path / "tmpdir").glob("install-local-marketplace-claude-snap.*"))


def test_c7_a_failing_claude_add_keeps_its_exit_code_and_is_still_reported(tmp_path):
    # `claude` itself decides the exit code (the script runs under set -e): the reporting step
    # must neither swallow a failure nor turn a success into one.
    bad_home = tmp_path / "bad"
    bad_home.mkdir()
    bad, _ = _install(bad_home, claude_exit=3)
    assert bad.returncode == 3, bad.stdout + bad.stderr
    assert "engram-local.autoUpdate" in bad.stdout


def test_c8_skip_add_takes_no_snapshot_and_prints_no_report(tmp_path):
    res, claude_home = _install(tmp_path, extra_args=("--skip-add",))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "autoUpdate" not in res.stdout
    assert not (tmp_path / "install-backups").exists()
    assert not (claude_home / "stub-claude-calls.log").exists()
    assert "claude-config" not in res.stdout and "marketplace add" not in res.stdout.replace(
        "claude plugin marketplace add", "")


def test_c8_no_claude_on_path_takes_no_snapshot_and_prints_no_report(tmp_path):
    res, claude_home = _install(tmp_path, with_claude=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cannot register the marketplace automatically" in res.stdout
    assert "autoUpdate" not in res.stdout
    assert not (tmp_path / "install-backups").exists()


def test_c8_codex_target_takes_no_snapshot(tmp_path):
    res = subprocess.run(
        ["bash", str(INSTALLER), "--target", "codex", "--skip-add", "--allow-branch", "--allow-untagged"],
        cwd=REPO_ROOT,
        env={**os.environ, "ENGRAM_HOME": str(tmp_path), "HOME": str(_login_home(tmp_path)),
             "CLAUDE_CONFIG_DIR": str(tmp_path / "cc")},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert "claude-config" not in res.stdout
    assert not (tmp_path / "install-backups").exists()
    assert not (tmp_path / "cc").exists()
