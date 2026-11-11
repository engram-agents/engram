"""bootstrap.py must not clobber a user's existing identity file (engram-alpha#4440).

Contract (written before the implementation):
  * One shared existence helper, `_target_present`, built on os.path.lexists, is used
    by the Claude path and the Codex path.
  * Claude: an existing CLAUDE.md that differs from the render makes bootstrap refuse
    (non-zero, one plain line naming the file and the two options), BEFORE anything
    else is written; the file stays byte-identical. An identical file is no refusal.
  * `--force` backs the file up to CLAUDE.md.pre-engram (copy2, once, never over an
    existing backup) and then overwrites. Same name as install.sh's backup.
  * A dangling symlink is "present": refused with and without --force.
  * Codex: config.toml is backed up once to config.toml.pre-engram before the first
    rewrite; the merge keeps foreign stanzas; a dangling symlink is refused.
  * install.sh passes --force to bootstrap only on the path where CLAUDE.md exists
    and its own consent/backup step succeeded.
  * uninstall: current CLAUDE.md is copied to CLAUDE.md.pre-uninstall.<UTC> first,
    then the pre-engram backup is restored; with no backup, CLAUDE.md is untouched.

All runs use tmp homes through env; the real ~/.claude, ~/.codex, ~/.engram are never read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
BOOTSTRAP = SRC / "bootstrap.py"
INSTALL_SH = SRC / "tools" / "install.sh"
UNINSTALL_SH = SRC / "tools" / "uninstall-local-marketplace.sh"

RUNTIME_FILES = [
    "server.py", "engram_client.py", "engram_confidence.py",
    "engram_filter.py", "engram_idf.py", "engram_ids.py",
    "engram_log_emitter.py", "engram_log_indexer.py",
]


def _home(tmp_path: Path) -> Path:
    engram_home = tmp_path / ".engram"
    engram_home.mkdir(parents=True, exist_ok=True)
    for fname in RUNTIME_FILES:
        if (SRC / fname).exists():
            shutil.copy2(SRC / fname, engram_home / fname)
    return tmp_path


def _run(tmp_path: Path, target: str = "claude", args: list[str] | None = None):
    _home(tmp_path)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(tmp_path / ".engram"),
        "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "PYTHON_BIN": sys.executable,
        "TARGET": target,
    })
    for k in ("ENGRAM_BOOTSTRAP", "FORCE", "FORCE_RESEED_EMPTY"):
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, str(BOOTSTRAP), *(args or [])],
        env=env, capture_output=True, text=True, timeout=120,
    )


def _db(tmp_path: Path) -> Path:
    return tmp_path / ".engram" / "knowledge.db"


# ---------------------------------------------------------------- Claude path

def test_claude_no_file_is_written_without_flag(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / ".claude" / "CLAUDE.md").is_file()
    assert not (tmp_path / ".claude" / "CLAUDE.md.pre-engram").exists()


def test_claude_existing_differing_file_refused_and_untouched(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"my own instructions\n")
    r = _run(tmp_path)
    assert r.returncode != 0
    assert md.read_bytes() == b"my own instructions\n"
    msg = r.stderr + r.stdout
    assert str(md) in msg and "--force" in msg and "CLAUDE.md.pre-engram" in msg
    assert "move" in msg
    assert not (tmp_path / ".claude" / "CLAUDE.md.pre-engram").exists()


def test_claude_refusal_happens_before_anything_is_written(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"mine\n")
    r = _run(tmp_path)
    assert r.returncode != 0
    assert not _db(tmp_path).exists(), "refusal must precede seeding"
    for name in ("seed-manifest.json", "warm-briefing.md", "first-run-pending"):
        assert not (tmp_path / ".engram" / name).exists(), name


def test_claude_refusal_is_one_line(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"mine\n")
    r = _run(tmp_path)
    lines = [ln for ln in (r.stderr + r.stdout).splitlines() if "REFUSED" in ln]
    assert len(lines) == 1, lines


def test_claude_refusal_then_force_rerun_succeeds(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"mine\n")
    assert _run(tmp_path).returncode != 0
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_claude_late_guard_refusal_says_db_was_seeded_and_gives_recovery(tmp_path):
    """The late (post-seeding) check's refusal must not read like the early one.

    Late path, constructed from the outside: the predicted seed IDs are replaced by made-up
    ones and CLAUDE.md is planted as exactly that prediction, so the check-only pass finds it
    identical; the real seeding then mints the real IDs, so the second check refuses.
    """
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    r = _probe(tmp_path, (
        "import os\nos.environ['PYTHON_BIN'] = sys.executable\n"
        "bootstrap._FIXED_SEED_IDS = {k: v[:3] + str(9001 + i) for i, (k, v) in enumerate(bootstrap._FIXED_SEED_IDS.items())}\n"
        "Path(sys.argv[1]).write_text(bootstrap._predicted_claude_render())\n"
        "sys.argv = ['bootstrap.py']\n"
        "bootstrap.main()\n"
    ), str(md))
    assert r.returncode != 0, r.stdout + r.stderr
    planted = md.read_text()
    assert "9001" in planted, "the planted file is not the made-up prediction"
    msg = r.stderr + r.stdout
    assert msg.count("REFUSED") == 1, msg
    assert "[bootstrap] REFUSED:" in msg
    assert str(md) in msg
    assert "already seeded" in msg
    assert str(_db(tmp_path)) in msg
    assert "--force" in msg and "move" in msg


def test_claude_identical_existing_file_is_not_refused(tmp_path):
    first = _run(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    md = tmp_path / ".claude" / "CLAUDE.md"
    rendered = md.read_bytes()
    # A retried install over an emptied data home: same render.
    shutil.rmtree(tmp_path / ".engram")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert md.read_bytes() == rendered
    assert not (tmp_path / ".claude" / "CLAUDE.md.pre-engram").exists()


def test_claude_force_backs_up_with_metadata_then_overwrites(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"mine\n")
    os.utime(md, (1_000_000_000, 1_000_000_000))
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    bak = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    assert bak.read_bytes() == b"mine\n"
    assert int(bak.stat().st_mtime) == 1_000_000_000, "copy2 keeps mtime"
    assert md.read_bytes() != b"mine\n"
    assert str(bak) in r.stdout


def test_claude_force_never_touches_existing_backup(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"current\n")
    bak = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    bak.write_bytes(b"first original\n")
    os.utime(bak, (1_100_000_000, 1_100_000_000))
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert bak.read_bytes() == b"first original\n"
    assert int(bak.stat().st_mtime) == 1_100_000_000
    assert md.read_bytes() != b"current\n"


def test_claude_dangling_symlink_refused_without_force(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.symlink_to(tmp_path / "nowhere.md")
    r = _run(tmp_path)
    assert r.returncode != 0
    assert md.is_symlink() and not (tmp_path / "nowhere.md").exists()


def test_claude_dangling_symlink_refused_even_with_force(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.symlink_to(tmp_path / "nowhere.md")
    r = _run(tmp_path, args=["--force"])
    assert r.returncode != 0
    assert not (tmp_path / "nowhere.md").exists(), "never write through the link"
    assert md.is_symlink()
    assert "symlink" in (r.stderr + r.stdout)
    assert not _db(tmp_path).exists()


def _stamped_backups(parent: Path, name: str):
    return sorted(p for p in parent.iterdir() if p.name.startswith(name + ".engram-backup."))


def test_claude_dangling_backup_link_is_left_alone_and_a_stamped_backup_is_made(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"my own instructions\n")
    bak = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    gone = tmp_path / "nowhere-backup.md"
    bak.symlink_to(gone)
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert bak.is_symlink() and not gone.exists(), "never write through the link"
    assert [p.read_bytes() for p in _stamped_backups(md.parent, "CLAUDE.md")] == [b"my own instructions\n"]
    assert md.read_bytes() != b"my own instructions\n"


def test_claude_directory_at_backup_path_is_left_alone_and_a_stamped_backup_is_made(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"my own instructions\n")
    bak = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    bak.mkdir()
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert bak.is_dir() and not list(bak.iterdir())
    assert [p.read_bytes() for p in _stamped_backups(md.parent, "CLAUDE.md")] == [b"my own instructions\n"]
    assert md.read_bytes() != b"my own instructions\n"


def test_claude_valid_backup_symlink_counts_as_existing_backup(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"current\n")
    real = tmp_path / "real-backup.md"
    real.write_bytes(b"first original\n")
    bak = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    bak.symlink_to(real)
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert real.read_bytes() == b"first original\n"
    assert bak.is_symlink()
    assert [p.read_bytes() for p in _stamped_backups(md.parent, "CLAUDE.md")] == [b"current\n"]
    assert md.read_bytes() != b"current\n"


def _assert_refused_for_obstructed_backup(tmp_path, r, target, bak, original):
    """The backup name is the same file as the target: refuse, write nothing, name the remedy."""
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert out.count("REFUSED") == 1 and str(bak) in out and "same file" in out
    assert "Move that backup name aside" in out
    assert target.read_bytes() == original and bak.read_bytes() == original
    assert not _stamped_backups(target.parent, target.name), "no stamped backup on a refusal"
    assert not _db(tmp_path).exists(), "nothing is seeded behind a refusal"


def test_claude_hard_link_at_backup_path_is_refused_even_with_force(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"my own instructions\n")
    bak = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    os.link(md, bak)
    r = _run(tmp_path, args=["--force"])
    _assert_refused_for_obstructed_backup(tmp_path, r, md, bak, b"my own instructions\n")


def _probe(tmp_path: Path, body: str, *argv: str) -> subprocess.CompletedProcess:
    """Run `body` in a child that has imported bootstrap (not as __main__) under tmp env."""
    _home(tmp_path)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(tmp_path / ".engram"),
        "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "TARGET": "claude",
    })
    code = (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "import bootstrap\n" + body
    )
    return subprocess.run([sys.executable, "-c", code, *argv],
                          env=env, capture_output=True, text=True, timeout=120)


def test_copy_exclusive_refuses_existing_file_and_never_truncates_it(tmp_path):
    src = tmp_path / "src.md"
    src.write_bytes(b"source bytes\n")
    dst = tmp_path / "dst.md"
    dst.write_bytes(b"someone else's file, must not be truncated\n")
    r = _probe(tmp_path, (
        "try:\n"
        "    bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "except FileExistsError:\n"
        "    print('FILE_EXISTS')\n"
    ), str(src), str(dst))
    assert r.returncode == 0, r.stderr
    assert "FILE_EXISTS" in r.stdout, r.stdout + r.stderr
    assert dst.read_bytes() == b"someone else's file, must not be truncated\n"
    assert src.read_bytes() == b"source bytes\n"


def test_copy_exclusive_does_not_write_through_a_symlink_at_dst(tmp_path):
    src = tmp_path / "src.md"
    src.write_bytes(b"source bytes\n")
    victim = tmp_path / "victim.md"
    victim.write_bytes(b"victim\n")
    dst = tmp_path / "dst.md"
    dst.symlink_to(victim)
    r = _probe(tmp_path, (
        "try:\n"
        "    bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "except FileExistsError:\n"
        "    print('FILE_EXISTS')\n"
    ), str(src), str(dst))
    assert "FILE_EXISTS" in r.stdout, r.stdout + r.stderr
    assert victim.read_bytes() == b"victim\n"


def test_copy_exclusive_copies_bytes_and_metadata_to_an_absent_dst(tmp_path):
    src = tmp_path / "src.md"
    src.write_bytes(b"source bytes\n")
    os.utime(src, (1_000_000_000, 1_000_000_000))
    os.chmod(src, 0o640)
    dst = tmp_path / "dst.md"
    r = _probe(tmp_path, "bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n",
               str(src), str(dst))
    assert r.returncode == 0, r.stderr
    assert dst.read_bytes() == b"source bytes\n"
    assert int(dst.stat().st_mtime) == 1_000_000_000
    assert (dst.stat().st_mode & 0o777) == 0o640


# Atomic publish: after _backup_once returns or raises, the backup path is absent or a
# COMPLETE copy. A partial file there would read as "backup exists" on the next run.

_FAIL_COPY = (
    "import errno, shutil\n"
    "def _fail(fsrc, fdst, *a, **k):\n"
    "    fdst.write(fsrc.read(4))\n"
    "    fdst.flush()\n"
    "    raise OSError(errno.ENOSPC, 'No space left on device')\n"
    "shutil.copyfileobj = _fail\n"
)


def _refused_lines(r):
    return [ln for ln in (r.stderr + r.stdout).splitlines() if "REFUSED" in ln]


def test_copy_exclusive_midcopy_failure_leaves_no_dst_and_no_partial(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    src = d / "src.md"
    src.write_bytes(b"source bytes that are longer than four\n")
    dst = d / "dst.md"
    r = _probe(tmp_path, _FAIL_COPY + (
        "try:\n"
        "    bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "except OSError as e:\n"
        "    print('RAISED', e.errno)\n"
    ), str(src), str(dst))
    assert r.returncode == 0, r.stderr
    assert "RAISED" in r.stdout, r.stdout + r.stderr
    assert not os.path.lexists(dst), "partial file left at the backup path"
    assert sorted(p.name for p in d.iterdir()) == ["src.md"], list(d.iterdir())


def test_force_midcopy_failure_refuses_keeps_claude_md_then_rerun_backs_up_exactly(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"my own instructions, long enough to be cut\n")
    bak = md.parent / "CLAUDE.md.pre-engram"
    r = _probe(tmp_path, _FAIL_COPY + (
        "import os\nos.environ['PYTHON_BIN'] = sys.executable\n"
        "sys.argv = ['bootstrap.py', '--force']\n"
        "bootstrap.main()\n"
    ))
    assert r.returncode != 0, r.stdout + r.stderr
    lines = _refused_lines(r)
    assert len(lines) == 1, lines
    assert lines[0].startswith("[bootstrap] REFUSED:") and str(bak) in lines[0]
    assert md.read_bytes() == b"my own instructions, long enough to be cut\n"
    assert not os.path.lexists(bak)
    assert not list(md.parent.glob("*.partial-*"))
    assert not _db(tmp_path).exists(), "the backup failed before anything was seeded"
    r2 = _run(tmp_path, args=["--force"])
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert bak.read_bytes() == b"my own instructions, long enough to be cut\n"
    assert md.read_bytes() != b"my own instructions, long enough to be cut\n"


def test_copy_exclusive_eperm_link_falls_back_to_publishing_a_complete_copy(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    src = d / "src.md"
    src.write_bytes(b"source bytes\n")
    os.utime(src, (1_000_000_000, 1_000_000_000))
    os.chmod(src, 0o640)
    dst = d / "dst.md"
    r = _probe(tmp_path, (
        "import errno, os\n"
        "def _nolink(a, b, *x, **k):\n"
        "    raise OSError(errno.EPERM, 'Operation not permitted')\n"
        "os.link = _nolink\n"
        "bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    ), str(src), str(dst))
    assert r.returncode == 0, r.stderr
    assert dst.read_bytes() == b"source bytes\n"
    assert int(dst.stat().st_mtime) == 1_000_000_000
    assert (dst.stat().st_mode & 0o777) == 0o640
    assert sorted(p.name for p in d.iterdir()) == ["dst.md", "src.md"], list(d.iterdir())


def test_copy_exclusive_unexpected_link_error_refuses_and_cleans_up(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    src = d / "CLAUDE.md"
    src.write_bytes(b"source bytes\n")
    bak = d / "CLAUDE.md.pre-engram"
    r = _probe(tmp_path, (
        "import errno, os\n"
        "def _eio(a, b, *x, **k):\n"
        "    raise OSError(errno.EIO, 'Input/output error')\n"
        "os.link = _eio\n"
        "bootstrap._backup_once(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    ), str(src), str(bak))
    assert r.returncode != 0
    lines = _refused_lines(r)
    assert len(lines) == 1, lines
    assert lines[0].startswith("[bootstrap] REFUSED:") and str(bak) in lines[0]
    assert "Input/output error" in lines[0]
    assert not os.path.lexists(bak)
    assert sorted(p.name for p in d.iterdir()) == ["CLAUDE.md"], list(d.iterdir())


def test_backup_once_turns_a_file_that_appeared_into_a_one_line_refusal(tmp_path):
    src = tmp_path / "CLAUDE.md"
    src.write_bytes(b"source bytes\n")
    bak = tmp_path / "CLAUDE.md.pre-engram"
    bak.write_bytes(b"appeared in the window\n")
    r = _probe(tmp_path, (
        "real = bootstrap.fg.target_present\n"
        "bootstrap.fg.target_present = lambda p: False if Path(p) == Path(sys.argv[2]) else real(p)\n"
        "bootstrap._backup_once(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    ), str(src), str(bak))
    assert r.returncode != 0
    lines = [ln for ln in (r.stderr + r.stdout).splitlines() if "REFUSED" in ln]
    assert len(lines) == 1, lines
    assert lines[0].startswith("[bootstrap] REFUSED:") and str(bak) in lines[0]
    assert bak.read_bytes() == b"appeared in the window\n"
    assert src.read_bytes() == b"source bytes\n"


def test_claude_symlink_to_target_at_backup_path_is_refused_even_with_force(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"my own instructions\n")
    bak = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    os.symlink(md, bak)
    r = _run(tmp_path, args=["--force"])
    _assert_refused_for_obstructed_backup(tmp_path, r, md, bak, b"my own instructions\n")
    assert bak.is_symlink()


# ------------------------------------------------------- shared helper (static)

def _fn_source(src: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |^if __name__)", src, re.S | re.M)
    assert m, f"{name} missing"
    return m.group(0)


def test_shared_helper_is_lexists_and_used_by_both_paths():
    src = (SRC / "engram_file_guard.py").read_text()
    assert "os.path.lexists" in _fn_source(src, "target_present")
    assert "target_present(" in _fn_source(src, "classify")
    assert "target_present(" in _fn_source(src, "_next_backup_name")
    boot = BOOTSTRAP.read_text()
    assert "fg.plan(" in boot and "fg.apply(" in boot, "bootstrap decides and writes through the guard"


def test_backup_name_is_the_installers_name():
    sh = INSTALL_SH.read_text()
    m = re.search(r'CLAUDE_MD_BACKUP="\$CLAUDE_HOME/([^"]+)"', sh)
    assert m and m.group(1) == "CLAUDE.md.pre-engram"
    py = BOOTSTRAP.read_text()
    assert f'"{m.group(1)}"' in py


def test_backup_name_matches_for_same_claude_home(tmp_path):
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(b"x\n")
    assert _run(tmp_path, args=["--force"]).returncode == 0
    names = sorted(p.name for p in (tmp_path / ".claude").iterdir() if "pre-engram" in p.name)
    assert names == ["CLAUDE.md.pre-engram"]


# ------------------------------------------------------------------ Codex path

def test_codex_backup_made_once_with_original_bytes(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    original = b'instructions = """\nmine\n"""\n[plugins.someone]\nkey = "v"\n'
    cfg.write_bytes(original)
    assert _run(tmp_path, target="codex").returncode != 0, "a differing config.toml needs consent"
    assert cfg.read_bytes() == original
    r = _run(tmp_path, target="codex", args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    bak = tmp_path / ".codex" / "config.toml.pre-engram"
    assert bak.read_bytes() == original
    # second run over an emptied data home: config.toml already holds what would be written,
    # so it is left alone and the backup is untouched
    shutil.rmtree(tmp_path / ".engram")
    mtime = bak.stat().st_mtime_ns
    time.sleep(0.01)
    r2 = _run(tmp_path, target="codex")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert bak.read_bytes() == original
    assert bak.stat().st_mtime_ns == mtime
    # only the first (consented) run saved anything: the second run wrote nothing
    assert len([p for p in cfg.parent.iterdir() if p.name.startswith("config.toml.engram-backup.")]) == 1


def test_codex_no_config_means_no_backup(tmp_path):
    r = _run(tmp_path, target="codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (tmp_path / ".codex" / "config.toml.pre-engram").exists()


def test_codex_foreign_stanza_survives_rerender_byte_for_byte(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    stanza = '[plugins.someone]\nkey = "value"\nother = 3\n'
    cfg.write_text(stanza)
    assert _run(tmp_path, target="codex", args=["--force"]).returncode == 0
    shutil.rmtree(tmp_path / ".engram")
    assert _run(tmp_path, target="codex").returncode == 0
    assert stanza in cfg.read_text()
    assert cfg.read_text().count('instructions = """') == 1


def test_codex_dangling_symlink_refused(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.symlink_to(tmp_path / "nowhere.toml")
    r = _run(tmp_path, target="codex")
    assert r.returncode != 0
    assert not (tmp_path / "nowhere.toml").exists()
    assert cfg.is_symlink()
    assert not _db(tmp_path).exists()
    assert "symlink" in (r.stderr + r.stdout)


def test_codex_dangling_backup_link_is_left_alone_and_a_stamped_backup_is_made(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_bytes(b'model = "x"\n')
    bak = tmp_path / ".codex" / "config.toml.pre-engram"
    gone = tmp_path / "nowhere-backup.toml"
    bak.symlink_to(gone)
    r = _run(tmp_path, target="codex", args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert bak.is_symlink() and not gone.exists()
    assert [p.read_bytes() for p in _stamped_backups(cfg.parent, "config.toml")] == [b'model = "x"\n']


def test_codex_directory_at_backup_path_is_left_alone_and_a_stamped_backup_is_made(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_bytes(b'model = "x"\n')
    bak = tmp_path / ".codex" / "config.toml.pre-engram"
    bak.mkdir()
    r = _run(tmp_path, target="codex", args=["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert bak.is_dir() and not list(bak.iterdir())
    assert [p.read_bytes() for p in _stamped_backups(cfg.parent, "config.toml")] == [b'model = "x"\n']


def test_codex_hard_link_at_backup_path_is_refused_even_with_force(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_bytes(b'model = "x"\n')
    bak = tmp_path / ".codex" / "config.toml.pre-engram"
    os.link(cfg, bak)
    r = _run(tmp_path, target="codex", args=["--force"])
    _assert_refused_for_obstructed_backup(tmp_path, r, cfg, bak, b'model = "x"\n')


# --------------------------------------------------------------- install.sh

def test_install_sh_passes_force_only_when_claude_md_exists_after_consent():
    sh = INSTALL_SH.read_text()
    # Every bootstrap invocation takes its args from one variable ...
    calls = re.findall(r'"\$VENV_PY" "\$PLUGIN_ROOT/bootstrap\.py"([^\n]*)', sh)
    assert len(calls) == 2, calls
    for tail in calls:
        assert "_bootstrap_flags" in tail, tail
    # ... which is set to --force in exactly one place, inside the existing-CLAUDE.md branch,
    # after the consent/backup `fail` guard and the backup copy.
    assigns = [m.start() for m in re.finditer(r'_bootstrap_flags=.*--force', sh)]
    assert len(assigns) == 1
    default = sh.index('_bootstrap_flags=""')
    consent = sh.index('exists and was not backed up')
    note = sh.index("printf '[step 6] CLAUDE.md: %s\\n'")
    assert default < consent < assigns[0] < note
    first_call = sh.index('"$VENV_PY" "$PLUGIN_ROOT/bootstrap.py"')
    assert note < first_call


# ---------------------------------------------------------------- uninstall

def _run_uninstall(tmp_path: Path, target: str = "claude-code", marketplace: bool = True,
                   args: tuple = ()):
    env = os.environ.copy()
    env.update({
        "ENGRAM_HOME": str(tmp_path / ".engram"),
        "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "HOME": str(tmp_path),
    })
    if marketplace:
        (tmp_path / ".engram" / "marketplace").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(UNINSTALL_SH), "--skip-cli", "--target", target, *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_uninstall_keeps_edited_render_then_restores_original(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "CLAUDE.md").write_bytes(b"agent's edited render\n")
    (home / "CLAUDE.md.pre-engram").write_bytes(b"user original\n")
    r = _run_uninstall(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    saved = [p for p in home.iterdir() if p.name.startswith("CLAUDE.md.pre-uninstall.")]
    assert len(saved) == 1
    assert re.fullmatch(r"CLAUDE\.md\.pre-uninstall\.\d{8}T\d{6}Z", saved[0].name)
    assert saved[0].read_bytes() == b"agent's edited render\n"
    assert (home / "CLAUDE.md").read_bytes() == b"user original\n"
    assert str(saved[0]) in r.stdout


def test_uninstall_without_backup_leaves_claude_md_alone_and_says_so(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "CLAUDE.md").write_bytes(b"agent's file\n")
    r = _run_uninstall(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "CLAUDE.md").read_bytes() == b"agent's file\n"
    assert not [p for p in home.iterdir() if "pre-uninstall" in p.name]
    assert "pre-engram" in r.stdout and "untouched" in r.stdout


def test_uninstall_codex_restores_config_toml_after_saving_current(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_bytes(b"current config\n")
    (home / "config.toml.pre-engram").write_bytes(b"original config\n")
    r = _run_uninstall(tmp_path, target="codex")
    assert r.returncode == 0, r.stdout + r.stderr
    saved = [p for p in home.iterdir() if p.name.startswith("config.toml.pre-uninstall.")]
    assert len(saved) == 1 and saved[0].read_bytes() == b"current config\n"
    assert (home / "config.toml").read_bytes() == b"original config\n"


def test_uninstall_claude_target_does_not_touch_codex(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_bytes(b"current config\n")
    (home / "config.toml.pre-engram").write_bytes(b"original config\n")
    _run_uninstall(tmp_path)
    assert (home / "config.toml").read_bytes() == b"current config\n"


# Marketplace tree and version marker already gone, backup still there: the restore must
# not sit behind the "Nothing to uninstall" early exit.

def test_uninstall_restores_even_when_marketplace_already_gone(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "CLAUDE.md").write_bytes(b"agent's edited render\n")
    (home / "CLAUDE.md.pre-engram").write_bytes(b"user original\n")
    r = _run_uninstall(tmp_path, marketplace=False)
    assert r.returncode == 0, r.stdout + r.stderr
    saved = [p for p in home.iterdir() if p.name.startswith("CLAUDE.md.pre-uninstall.")]
    assert len(saved) == 1 and saved[0].read_bytes() == b"agent's edited render\n"
    assert (home / "CLAUDE.md").read_bytes() == b"user original\n"


def test_uninstall_gone_marketplace_dry_run_writes_nothing(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "CLAUDE.md").write_bytes(b"agent's edited render\n")
    (home / "CLAUDE.md.pre-engram").write_bytes(b"user original\n")
    r = _run_uninstall(tmp_path, marketplace=False, args=("--dry-run",))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "CLAUDE.md").read_bytes() == b"agent's edited render\n"
    assert not [p for p in home.iterdir() if "pre-uninstall" in p.name]


def test_uninstall_gone_marketplace_dangling_claude_md_left_untouched(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "CLAUDE.md").symlink_to(tmp_path / "nowhere")
    (home / "CLAUDE.md.pre-engram").write_bytes(b"user original\n")
    r = _run_uninstall(tmp_path, marketplace=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "CLAUDE.md").is_symlink() and not (home / "CLAUDE.md").exists()
    assert not [p for p in home.iterdir() if "pre-uninstall" in p.name]


def test_uninstall_nothing_there_and_no_backup_is_unchanged(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "CLAUDE.md").write_bytes(b"agent's file\n")
    r = _run_uninstall(tmp_path, marketplace=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Nothing to uninstall" in r.stdout
    assert (home / "CLAUDE.md").read_bytes() == b"agent's file\n"
    assert not [p for p in home.iterdir() if "pre-uninstall" in p.name]
