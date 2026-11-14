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


def test_snapshot_never_overwrites_an_existing_snapshot_copy(rig):
    """The snapshot folder is fresh, so a copy already there is unexpected: it is left
    exactly as it was (never truncated), and the helper still exits 0 with its one line."""
    _jw(rig.f("k.json"), {"a": 1})
    kept = rig.snap / "files" / "k.json"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"EXISTING-LONGER-THAN-THE-NEW-COPY" * 4)
    before = kept.read_bytes()
    res = rig.snapshot("k.json")
    assert res.returncode == 0
    assert _lines(res) == [TAG + "claude-config diff unavailable: FileExistsError"]
    assert kept.read_bytes() == before


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
    if os.environ.get("STUB_DROP_SETTINGS_KEY"):
        reordered.pop(os.environ["STUB_DROP_SETTINGS_KEY"], None)
    tmp = sp + ".tmp-stub"
    with open(tmp, "w") as fh:
        json.dump(reordered, fh)
    os.replace(tmp, sp)
    sys.exit(int(os.environ.get("STUB_CLAUDE_EXIT", "0")))
'''


def _install(tmp_path, *, extra_args=(), with_claude=True, claude_exit=0, seed=True,
             env_extra=None, stubs_extra=None):
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
    for stub_name, stub_text in (stubs_extra or {}).items():
        (stubs / stub_name).write_text(stub_text)
        (stubs / stub_name).chmod(0o755)
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
    env.update(env_extra or {})
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


# ---------------------------------------------------------------- review round: gaps and fixes

def test_s2_a_key_name_cannot_forge_a_line_of_the_report(rig):
    evil = "a\n" + TAG + "FAKE backup kept at: /etc/passwd\r\x1b[2J z"
    _jw(rig.f("k.json"), {evil: 1, "keep": 1, "dotted.name/with/slash": 1})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"keep": 1, "dotted.name/with/slash": 1})
    res = rig.report("k.json")
    assert res.returncode == 0
    assert not [ln for ln in res.stdout.splitlines() if ln.startswith(TAG + "FAKE")]
    assert "\x1b" not in res.stdout and "\r" not in res.stdout and " " not in res.stdout
    assert "\\n" in res.stdout  # the newline is shown escaped, not dropped


def test_s2_ordinary_key_names_stay_readable(rig):
    _jw(rig.f("k.json"), {"a.b/c": {"d-e_f": 1}})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"a.b/c": {}})
    assert "a.b/c.d-e_f: removed" in rig.report("k.json").stdout


def test_s3a_removed_and_added_string_values_are_never_printed(rig):
    _jw(rig.f("k.json"), {"gone": "OLDTOKEN-" + SECRET, "keep": 1})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"keep": 1, "fresh": "NEWTOKEN-" + SECRET})
    res = rig.report("k.json")
    assert "gone: removed" in res.stdout and "fresh: added" in res.stdout
    assert "OLDTOKEN" not in res.stdout and "NEWTOKEN" not in res.stdout
    assert "SECRETVALUE" not in res.stdout + res.stderr


def test_s1_one_files_failure_does_not_hide_another_and_its_backup_is_kept_first(rig):
    _jw(rig.f("known.json"), {"mk": {"autoUpdate": True, "src": 1}})
    _jw(rig.f("settings.json"), {"model": "m"})
    rig.snapshot("known.json", "settings.json")
    _jw(rig.f("known.json"), {"mk": {"src": 1}})
    rig.f("settings.json").unlink()
    rig.f("settings.json").mkdir()  # reading it raises IsADirectoryError
    res = rig.report("known.json", "settings.json")
    assert res.returncode == 0
    out = res.stdout
    assert "mk.autoUpdate: removed" in out
    (d,) = rig.backup_dirs()
    assert json.loads((d / "known.json").read_text()) == {"mk": {"autoUpdate": True, "src": 1}}
    assert f"your previous known.json is kept at: {d / 'known.json'}" in out
    unavailable = [ln for ln in out.splitlines() if "claude-config diff unavailable: IsADirectoryError" in ln]
    assert unavailable == [TAG + "claude-config diff unavailable: IsADirectoryError"]
    # the first file's backup is announced BEFORE the second file's failure
    assert out.index("is kept at") < out.index("diff unavailable")


def test_s1_an_early_file_failing_does_not_stop_the_later_files(rig):
    _jw(rig.f("settings.json"), {"model": "m"})
    _jw(rig.f("known.json"), {"mk": {"autoUpdate": True}})
    rig.snapshot("settings.json", "known.json")
    rig.f("settings.json").unlink()
    rig.f("settings.json").mkdir()
    _jw(rig.f("known.json"), {"mk": {}})
    res = rig.report("settings.json", "known.json")
    assert res.returncode == 0
    assert "diff unavailable: IsADirectoryError" in res.stdout
    assert "mk.autoUpdate: removed" in res.stdout
    (d,) = rig.backup_dirs()
    assert (d / "known.json").is_file()


def test_s1_a_backup_that_cannot_be_written_is_said_not_silent(rig):
    _jw(rig.f("k.json"), {"mk": {"autoUpdate": True}})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"mk": {}})
    rig.backups.write_text("a file where the backup folder should be")
    res = rig.report("k.json")
    assert res.returncode == 0
    lines = res.stdout.splitlines()
    removed = next(i for i, ln in enumerate(lines) if "mk.autoUpdate: removed" in ln)
    assert lines[removed + 1].startswith(TAG + "no backup was kept (")
    assert "kept at" not in res.stdout


def test_s3d_two_backups_in_the_same_second_get_two_folders(rig, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_ccd4628", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_stamp", lambda: "20260101T000000Z")  # freeze the clock: same second, for certain
    a = mod._backup_dir(rig.backups)
    b = mod._backup_dir(rig.backups)
    assert a != b and a.is_dir() and b.is_dir()
    assert a.name == "pre-marketplace-add-20260101T000000Z" and b.name == a.name + "-1"


def test_s3d_freezing_the_helper_clock_does_not_leak_into_other_tests():
    # Contract: the same-second cell must leave the stdlib clock alone. It used to assign
    # over time.strftime for the whole process. Runs right after that cell, in file order.
    import time
    assert time.strftime("%Y") != "20260101T000000Z"
    assert time.strftime("%Y") in (str(time.gmtime().tm_year), str(time.localtime().tm_year))


def test_s3d_the_clock_patch_survives_a_combined_run_with_the_snapshot_cell():
    # The reviewer's reproduction: this file's cell and an unrelated clock-reading cell, one process.
    cell = "tests/test_engram_snapshot.py::TestJsonlSnapshotOutput::test_captured_at_is_utc_iso"
    me = "tests/test_claude_config_diff_4628.py::test_s3d_two_backups_in_the_same_second_get_two_folders"
    env = dict(os.environ, ENGRAM_NO_EMBEDDINGS="1", PYTHONDONTWRITEBYTECODE="1")
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", me, cell],
                         cwd=REPO_ROOT, env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert res.returncode == 0, res.stdout[-1500:]
    assert "2 passed" in res.stdout, res.stdout[-500:]


def test_s3d_two_real_reports_back_to_back_keep_both_backups(rig):
    for n in (1, 2):
        _jw(rig.f("k.json"), {"mk": {"autoUpdate": True, "n": n}})
        rig.snapshot("k.json")
        _jw(rig.f("k.json"), {"mk": {"n": n}})
        res = rig.report("k.json")
        assert "no backup was kept" not in res.stdout
    assert len(rig.backup_dirs()) == 2
    assert all((d / "k.json").is_file() for d in rig.backup_dirs())


def test_s3e_a_backup_root_whose_parents_do_not_exist_is_created(rig, tmp_path):
    rig.backups = tmp_path / "no" / "such" / "parents" / "install-backups"
    _jw(rig.f("k.json"), {"mk": {"autoUpdate": True}})
    rig.snapshot("k.json")
    _jw(rig.f("k.json"), {"mk": {}})
    res = rig.report("k.json")
    assert "no backup was kept" not in res.stdout
    (d,) = rig.backup_dirs()
    assert (d / "k.json").is_file()


_RM_STUB = """#!/bin/bash
for a in "$@"; do
  case "$(basename "$a")" in
    install-local-marketplace-claude-snap.*) echo "$a" >> "$(dirname "$0")/rm-refused.log"; exit 1 ;;
  esac
done
exec /bin/rm "$@"
"""


def test_b1_a_failing_snapshot_cleanup_does_not_change_the_exit_code(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    ok, _ = _install(base)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    for name, claude_exit in (("rm-ok-run", 0), ("rm-fail-claude", 3)):
        d = tmp_path / name
        d.mkdir()
        res, _ = _install(d, claude_exit=claude_exit, stubs_extra={"rm": _RM_STUB})
        # the stub really refused the snapshot folder (otherwise this cell proves nothing)
        assert (d / "stubbin" / "rm-refused.log").is_file()
        assert res.returncode == claude_exit, res.stdout + res.stderr
        assert "engram-local.autoUpdate" in res.stdout
        if claude_exit == 0:
            assert res.stdout.count("Marketplace registered. Next steps") == ok.stdout.count(
                "Marketplace registered. Next steps") == 1
            assert "ITEMS NOT DONE" not in res.stdout and "ERROR" not in res.stdout


_PY_STUB = """#!/bin/bash
case "$1" in
  *claude_config_diff.py)
    echo "$2" >> "$(dirname "$0")/helper-calls.log"
    case ",$STUB_HELPER_FAILS," in *",$2,"*) exit 7 ;; esac ;;
esac
exec {python} "$@"
"""


@pytest.mark.parametrize("failing", ["snapshot", "report", "snapshot,report"])
def test_s3b_a_helper_that_exits_nonzero_leaves_the_installer_exit_code_alone(tmp_path, failing):
    stub = _PY_STUB.format(python=sys.executable)
    res, _ = _install(tmp_path, env_extra={"STUB_HELPER_FAILS": failing}, stubs_extra={"python3": stub})
    calls = (tmp_path / "stubbin" / "helper-calls.log").read_text().split()
    assert "snapshot" in calls and "report" in calls  # the helper really ran, and really failed
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Marketplace registered. Next steps" in res.stdout


def test_s3c_both_files_are_watched_a_key_dropped_from_settings_is_reported(tmp_path):
    res, claude_home = _install(tmp_path, env_extra={"STUB_DROP_SETTINGS_KEY": "theme"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "settings.json was changed by the marketplace add:" in res.stdout
    assert "theme: removed" in res.stdout
    (d,) = sorted((tmp_path / "install-backups").glob("pre-marketplace-add-*"))
    assert json.loads((d / "settings.json").read_text())["theme"] == "dark"
    assert "SECRETVALUE" not in res.stdout + res.stderr
