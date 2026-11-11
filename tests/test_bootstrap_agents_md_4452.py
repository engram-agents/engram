"""bootstrap.py (TARGET=codex) must not clobber a user's existing ~/.codex/AGENTS.md
(engram-alpha#4452; the Codex counterpart of the #4440 CLAUDE.md guard).

Contract (written before the implementation):
  * Existing CODEX_HOME/AGENTS.md that differs from what this run would write, no --force:
    exit non-zero with ONE line starting `[bootstrap] REFUSED:` naming the file, `--force`,
    the backup path AGENTS.md.pre-engram and "move the file away yourself"; nothing is
    written anywhere (AGENTS.md byte-identical, no knowledge.db, config.toml unchanged).
    The decision happens before the first write of the run.
  * An identical existing file is not refused.
  * --force backs up through the shared `_backup_once` to AGENTS.md.pre-engram (bytes and
    metadata; an existing backup is never touched), then writes. A dangling symlink at
    AGENTS.md is refused even with --force; a hard link at the backup path is refused.
  * A late guard before the AGENTS.md render (after seeding) refuses with a message that
    names the already-seeded knowledge.db and the recovery.
  * uninstall --target codex restores AGENTS.md like config.toml: save the current file to
    AGENTS.md.pre-uninstall.<UTC>, then restore AGENTS.md.pre-engram; also when the
    marketplace is already gone; dry-run writes nothing; a dangling AGENTS.md is left alone.
  * _copy_exclusive uses a unique temp: nothing at the old fixed temp name is touched, no temp survives.

All runs use tmp homes through env; the real ~/.claude, ~/.codex, ~/.engram are never read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bootstrap_no_clobber_4440 import (  # noqa: E402
    REPO_ROOT, SRC, _db, _home, _refused_lines, _run, _run_uninstall,
)


def _codex(tmp_path: Path, args: list[str] | None = None):
    return _run(tmp_path, target="codex", args=args)


def _agents(tmp_path: Path) -> Path:
    return tmp_path / ".codex" / "AGENTS.md"


def _mk(tmp_path: Path, data: bytes = b"my own codex conventions\n") -> Path:
    md = _agents(tmp_path)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_bytes(data)
    return md


def _probe_codex(tmp_path: Path, body: str, *argv: str) -> subprocess.CompletedProcess:
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
        "TARGET": "codex",
    })
    for k in ("ENGRAM_BOOTSTRAP", "FORCE", "FORCE_RESEED_EMPTY"):
        env.pop(k, None)
    code = (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "import bootstrap\n" + body
    )
    return subprocess.run([sys.executable, "-c", code, *argv],
                          env=env, capture_output=True, text=True, timeout=120)


# ------------------------------------------------------------------ refusal

def test_agents_no_file_is_written_without_flag(tmp_path):
    r = _codex(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _agents(tmp_path).is_file()
    assert not (tmp_path / ".codex" / "AGENTS.md.pre-engram").exists()


def test_agents_existing_differing_file_refused_and_untouched(tmp_path):
    md = _mk(tmp_path)
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.write_bytes(b'model = "x"\n')
    r = _codex(tmp_path)
    assert r.returncode != 0
    assert md.read_bytes() == b"my own codex conventions\n"
    assert cfg.read_bytes() == b'model = "x"\n'
    assert not (tmp_path / ".codex" / "config.toml.pre-engram").exists()
    # The refusal is ONE message that names every path needing consent (AGENTS.md and
    # config.toml here), each with the backup it would get.
    msg = r.stderr + r.stdout
    assert msg.count("REFUSED") == 1, msg
    assert "[bootstrap] REFUSED:" in msg
    assert str(md) in msg and "--force" in msg and "AGENTS.md.pre-engram" in msg
    assert str(cfg) in msg and "config.toml.pre-engram" in msg
    assert "move" in msg
    assert not (tmp_path / ".codex" / "AGENTS.md.pre-engram").exists()


def test_agents_refusal_happens_before_anything_is_written(tmp_path):
    _mk(tmp_path)
    r = _codex(tmp_path)
    assert r.returncode != 0
    assert not _db(tmp_path).exists(), "refusal must precede seeding"
    for name in ("seed-manifest.json", "warm-briefing.md", "first-run-pending",
                 "codex-compact-prompt.md"):
        assert not (tmp_path / ".engram" / name).exists(), name
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_agents_refusal_then_force_rerun_succeeds(tmp_path):
    _mk(tmp_path)
    assert _codex(tmp_path).returncode != 0
    r = _codex(tmp_path, ["--force"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_agents_identical_existing_file_is_not_refused(tmp_path):
    first = _codex(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    md = _agents(tmp_path)
    rendered = md.read_bytes()
    shutil.rmtree(tmp_path / ".engram")  # a retried install over an emptied data home
    r = _codex(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert md.read_bytes() == rendered
    assert not (tmp_path / ".codex" / "AGENTS.md.pre-engram").exists()


# -------------------------------------------------------------------- force

def test_agents_force_backs_up_with_metadata_then_overwrites(tmp_path):
    md = _mk(tmp_path, b"mine\n")
    os.utime(md, (1_000_000_000, 1_000_000_000))
    r = _codex(tmp_path, ["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    bak = tmp_path / ".codex" / "AGENTS.md.pre-engram"
    assert bak.read_bytes() == b"mine\n"
    assert int(bak.stat().st_mtime) == 1_000_000_000
    assert md.read_bytes() != b"mine\n"
    assert str(bak) in r.stdout


def test_agents_force_never_touches_existing_backup(tmp_path):
    _mk(tmp_path, b"current\n")
    bak = tmp_path / ".codex" / "AGENTS.md.pre-engram"
    bak.write_bytes(b"first original\n")
    os.utime(bak, (1_100_000_000, 1_100_000_000))
    r = _codex(tmp_path, ["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert bak.read_bytes() == b"first original\n"
    assert int(bak.stat().st_mtime) == 1_100_000_000
    assert _agents(tmp_path).read_bytes() != b"current\n"
    stamped = [p for p in bak.parent.iterdir() if p.name.startswith("AGENTS.md.engram-backup.")]
    assert [p.read_bytes() for p in stamped] == [b"current\n"], "the CURRENT file gets its own stamped backup"


def test_agents_dangling_symlink_refused_without_force(tmp_path):
    md = _agents(tmp_path)
    md.parent.mkdir()
    md.symlink_to(tmp_path / "nowhere.md")
    r = _codex(tmp_path)
    assert r.returncode != 0
    assert md.is_symlink() and not (tmp_path / "nowhere.md").exists()
    assert "symlink" in (r.stderr + r.stdout)
    assert not _db(tmp_path).exists()


def test_agents_dangling_symlink_refused_even_with_force(tmp_path):
    md = _agents(tmp_path)
    md.parent.mkdir()
    md.symlink_to(tmp_path / "nowhere.md")
    r = _codex(tmp_path, ["--force"])
    assert r.returncode != 0
    assert not (tmp_path / "nowhere.md").exists(), "never write through the link"
    assert md.is_symlink()
    assert "symlink" in (r.stderr + r.stdout)
    assert not _db(tmp_path).exists()


def test_agents_hard_link_at_backup_path_is_refused_even_with_force(tmp_path):
    """`AGENTS.md.pre-engram` that is the same file as AGENTS.md would be overwritten with it:
    refuse, write nothing, say to move that backup name aside."""
    md = _mk(tmp_path)
    bak = tmp_path / ".codex" / "AGENTS.md.pre-engram"
    os.link(md, bak)
    r = _codex(tmp_path, ["--force"])
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert out.count("REFUSED") == 1 and str(bak) in out and "same file" in out
    assert "Move that backup name aside" in out
    assert [p for p in bak.parent.iterdir() if p.name.startswith("AGENTS.md.engram-backup.")] == []
    assert md.read_bytes() == b"my own codex conventions\n" and bak.read_bytes() == b"my own codex conventions\n"
    assert not _db(tmp_path).exists()


def test_agents_force_dangling_config_refuses_before_any_backup(tmp_path):
    """Every early refusal precedes EITHER backup: a --force run must not leave
    AGENTS.md.pre-engram behind a refusal that then says nothing was written."""
    md = _mk(tmp_path)
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.symlink_to(tmp_path / "nowhere.toml")
    r = _codex(tmp_path, ["--force"])
    assert r.returncode != 0
    lines = _refused_lines(r)
    assert len(lines) == 1, lines
    assert lines[0].startswith("[bootstrap] REFUSED:"), lines[0]
    assert not (tmp_path / ".codex" / "AGENTS.md.pre-engram").exists()
    assert not (tmp_path / ".codex" / "config.toml.pre-engram").exists()
    assert md.read_bytes() == b"my own codex conventions\n"
    assert not _db(tmp_path).exists()


def test_agents_force_directory_at_config_backup_name_gets_a_stamped_backup(tmp_path):
    """A directory at config.toml.pre-engram is not a usable first backup: it is left alone and
    config.toml is saved to a stamped name instead."""
    md = _mk(tmp_path)
    (tmp_path / ".codex" / "config.toml").write_bytes(b'model = "x"\n')
    (tmp_path / ".codex" / "config.toml.pre-engram").mkdir()
    r = _codex(tmp_path, ["--force"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / ".codex" / "config.toml.pre-engram").is_dir()
    stamped = [p for p in (tmp_path / ".codex").iterdir() if p.name.startswith("config.toml.engram-backup.")]
    assert [p.read_bytes() for p in stamped] == [b'model = "x"\n']
    assert (tmp_path / ".codex" / "AGENTS.md.pre-engram").read_bytes() == b"my own codex conventions\n"
    assert md.read_bytes() != b"my own codex conventions\n"


def test_agents_template_has_no_seed_placeholders(tmp_path):
    """Static pin (not parameterised): the --force skip of the late guard and the exactness
    of `_predicted_agents_render` assume template.AGENTS.md has no seed-ID placeholder."""
    r = _probe_codex(tmp_path, "print(chr(10).join(bootstrap._SEED_PLACEHOLDERS))")
    assert r.returncode == 0, r.stdout + r.stderr
    tokens = [t for t in r.stdout.split("\n") if t]
    assert tokens, "could not read _SEED_PLACEHOLDERS"
    text = (SRC / "templates" / "template.AGENTS.md").read_text()
    present = [t for t in tokens if t in text]
    assert not present, (
        f"template.AGENTS.md contains seed placeholder(s) {present}: the early prediction "
        "could no longer match the real render, so every existing AGENTS.md would read as "
        "differing and the --force late-guard skip would be unsound. Teach "
        "_predicted_agents_render the seed IDs (as _predicted_claude_render does) first."
    )


# --------------------------------------------------------------- late guard

def test_agents_late_guard_refusal_says_db_was_seeded_and_gives_recovery(tmp_path):
    """Late path constructed by making the early PREDICTION equal the user's file while the
    real render differs; the early guard then passes and the late guard must refuse."""
    md = _mk(tmp_path, b"x")
    r = _probe_codex(tmp_path, (
        "bootstrap._predicted_agents_render = lambda: 'x'\n"
        "sys.argv = ['bootstrap']\n"
        "bootstrap.main()\n"
    ))
    assert r.returncode != 0, r.stdout + r.stderr
    assert md.read_bytes() == b"x"
    msg = r.stderr + r.stdout
    assert msg.count("REFUSED") == 1, msg
    assert "[bootstrap] REFUSED:" in msg
    assert str(md) in msg and "already seeded" in msg
    assert str(_db(tmp_path)) in msg
    assert "--force" in msg and "move" in msg


# ---------------------------------------------------------------- uninstall

def test_uninstall_codex_restores_agents_md_after_saving_current(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"current agents\n")
    (home / "AGENTS.md.pre-engram").write_bytes(b"original agents\n")
    r = _run_uninstall(tmp_path, target="codex")
    assert r.returncode == 0, r.stdout + r.stderr
    saved = [p for p in home.iterdir() if p.name.startswith("AGENTS.md.pre-uninstall.")]
    assert len(saved) == 1 and saved[0].read_bytes() == b"current agents\n"
    assert (home / "AGENTS.md").read_bytes() == b"original agents\n"
    assert (home / "AGENTS.md.pre-engram").read_bytes() == b"original agents\n"


def test_uninstall_codex_restores_agents_md_when_marketplace_already_gone(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"current agents\n")
    (home / "AGENTS.md.pre-engram").write_bytes(b"original agents\n")
    r = _run_uninstall(tmp_path, target="codex", marketplace=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Nothing to uninstall" in r.stdout
    assert (home / "AGENTS.md").read_bytes() == b"original agents\n"
    assert len([p for p in home.iterdir() if p.name.startswith("AGENTS.md.pre-uninstall.")]) == 1


def test_uninstall_codex_agents_md_dry_run_writes_nothing(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"current agents\n")
    (home / "AGENTS.md.pre-engram").write_bytes(b"original agents\n")
    r = _run_uninstall(tmp_path, target="codex", args=("--dry-run",))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "AGENTS.md").read_bytes() == b"current agents\n"
    assert not [p for p in home.iterdir() if "pre-uninstall" in p.name]


def test_uninstall_codex_dangling_agents_md_left_untouched(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "AGENTS.md").symlink_to(tmp_path / "nowhere")
    (home / "AGENTS.md.pre-engram").write_bytes(b"original agents\n")
    r = _run_uninstall(tmp_path, target="codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "AGENTS.md").is_symlink() and not (home / "AGENTS.md").exists()
    assert not [p for p in home.iterdir() if "pre-uninstall" in p.name]


def test_uninstall_claude_target_does_not_touch_agents_md(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"current agents\n")
    (home / "AGENTS.md.pre-engram").write_bytes(b"original agents\n")
    _run_uninstall(tmp_path)
    assert (home / "AGENTS.md").read_bytes() == b"current agents\n"


# --------------------------------------------------------------- stale temp

def _plant_then_copy(tmp_path, kind):
    src = tmp_path / "src.md"
    src.write_bytes(b"source bytes\n")
    dst = tmp_path / "dst.md"
    r = _probe_codex(tmp_path, (
        "import os\n"
        "src, dst, kind = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]\n"
        "old = Path(f'{dst}.partial-{os.getpid()}')\n"
        "if kind == 'dir':\n"
        "    old.mkdir()\n"
        "else:\n"
        "    old.write_bytes(b'someone else file')\n"
        "bootstrap._copy_exclusive(src, dst)\n"
        "print('OK')\n"
    ), str(src), str(dst), kind)
    return r, dst


def test_copy_exclusive_leaves_a_directory_at_the_old_fixed_temp_name_alone(tmp_path):
    r, dst = _plant_then_copy(tmp_path, "dir")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout
    assert dst.read_bytes() == b"source bytes\n"
    planted = [p for p in tmp_path.glob("dst.md.partial-*")]
    assert len(planted) == 1 and planted[0].is_dir(), planted


def test_copy_exclusive_leaves_a_file_at_the_old_fixed_temp_name_alone(tmp_path):
    r, dst = _plant_then_copy(tmp_path, "file")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout
    assert dst.read_bytes() == b"source bytes\n"
    planted = [p for p in tmp_path.glob("dst.md.partial-*")]
    assert len(planted) == 1 and planted[0].is_file(), planted
    assert planted[0].read_bytes() == b"someone else file"


_ENOSPC_COPY = (
    "import errno, shutil\n"
    "def _fail(fsrc, fdst, *a, **k):\n"
    "    fdst.write(fsrc.read(4))\n"
    "    fdst.flush()\n"
    "    raise OSError(errno.ENOSPC, 'No space left on device')\n"
    "shutil.copyfileobj = _fail\n"
)


def test_copy_exclusive_leaves_no_temp_after_success(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    src = d / "src.md"
    src.write_bytes(b"source bytes\n")
    dst = d / "dst.md"
    pre = d / "dst.md.partial-planted"
    pre.write_bytes(b"pre-existing")
    before = sorted(p.name for p in d.iterdir())
    r = _probe_codex(tmp_path, "bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n",
                     str(src), str(dst))
    assert r.returncode == 0, r.stdout + r.stderr
    assert dst.read_bytes() == b"source bytes\n"
    after = sorted(p.name for p in d.iterdir())
    assert after == sorted(before + ["dst.md"]), (before, after)


def test_copy_exclusive_leaves_no_temp_after_midcopy_enospc(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    src = d / "src.md"
    src.write_bytes(b"source bytes longer than four\n")
    dst = d / "dst.md"
    pre = d / "dst.md.partial-planted"
    pre.write_bytes(b"pre-existing")
    before = sorted(p.name for p in d.iterdir())
    r = _probe_codex(tmp_path, _ENOSPC_COPY + (
        "try:\n"
        "    bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "except OSError as e:\n"
        "    print('RAISED', e.errno)\n"
    ), str(src), str(dst))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RAISED" in r.stdout, r.stdout + r.stderr
    assert not dst.exists() and not dst.is_symlink()
    assert sorted(p.name for p in d.iterdir()) == before
    assert pre.read_bytes() == b"pre-existing"


def test_copy_exclusive_closes_the_temp_fd_when_src_cannot_be_opened(tmp_path):
    # mkstemp hands back an open fd; if opening src then fails, that fd must still be
    # closed (and the temp unlinked). /dev/fd lists open fds on Linux and macOS.
    d = tmp_path / "work"
    d.mkdir()
    r = _probe_codex(tmp_path, (
        "import os\nn = len(os.listdir('/dev/fd'))\n"
        "for _ in range(20):\n"
        "    try:\n"
        "        bootstrap._copy_exclusive(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "    except FileNotFoundError:\n"
        "        pass\n"
        "print('FD_DELTA', len(os.listdir('/dev/fd')) - n)\n"
    ), str(d / "missing.md"), str(d / "dst.md"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FD_DELTA 0" in r.stdout, r.stdout + r.stderr
    assert list(d.iterdir()) == []
