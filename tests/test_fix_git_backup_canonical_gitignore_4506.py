"""engram-alpha#4506 -- src/engram/tools/engram-fix-git-backup.sh takes its .gitignore from engram_core.

The one-time repair used to hold a 14-line literal `GITIGNORE_CONTENT` and write it over
`$ENGRAM_HOME/.gitignore`, which removed every protection added since (`secrets/`, `*api-key*`,
`forum-token`, `backups/`, `*.db.*`, `*.bak-*`, ...). Step 2 now calls
`engram_core._ensure_engram_gitignore(Path(ENGRAM_HOME))` and so inherits its semantics: an already-complete
file is left untouched (hand lines survive), a rewrite preserves `!`-negations.

Contract (written before the fix; A2, A3, A7 and B-as-mutated were red on the old script):

  A1  absent .gitignore, live run -> the written non-empty, non-`!` lines == list(ENGRAM_GITIGNORE_PATTERNS).
  A2  the OLD 14-line literal pre-existing -> after the run the lines == the constant.
  A3  a complete .gitignore + a hand line + a `!` negation -> bytes unchanged.
  A4  --dry-run -> no .gitignore created; stdout names every pattern of the constant.
  A5  engram_core.py missing at `<tools>/..` -> non-zero, the message names the path, nothing written
      (no .gitignore, no knowledge.sql, .git untouched).
  A6  `_ensure_engram_gitignore` swallows OSError; an unwritable .gitignore (a directory) must still die
      non-zero BEFORE step 3 (no knowledge.sql, no new git history).
  A7  a home WITH session_log.md (which the canonical list ignores) -> rc 0, a root commit that omits
      session_log.md and has the rest, and the skip is named in stdout (Step 5 must not `git add` an
      ignored path, after it has already removed the old .git).
  N1  a decoy engram_core.py in the run's cwd is never imported (sys.path.insert(0), not append), live and dry.
  N2  an ignored file under diary/ (diary/diary.db) is skipped by the diary loop's own check-ignore block.
  N3  `git check-ignore` failing (rc 128, via a git shim first on PATH) dies loudly, with no commit.
  B   drift guard (`find_second_copies`): no file under `src/` or `tools/` other than
      src/engram/engram_core.py holds a second literal copy of the list. A "copy" is a file in which at least
      MIN_COPY_ENTRIES (10) of the CURRENT canonical entries each occur as a whole line or as a whole quoted
      string. (The old 14-line literal holds 14, so it counts; the first threshold, 4 distinctive new entries,
      would have missed it.) tests/ is not scanned (it legitimately holds old lists). The scanned root is a
      parameter.

Fixtures and helpers are imported from tests/test_fix_git_backup_owner_snapshot_2652.py (not copied).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_fix_git_backup_owner_snapshot_2652 import (  # noqa: F401  (sandbox is a fixture)
    ENGRAM_BACKUP, SCRIPT, git, make_store, run_script, sandbox, seed_git,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402

PATTERNS = list(engram_core.ENGRAM_GITIGNORE_PATTERNS)

# The pre-#4506 literal (the one place the old list is allowed to live).
OLD_LITERAL = """*.db
*.db-shm
*.db-wal
*.db-journal
*?mode=*
*.bak
*.bak.*
config.json.bak*
*.backfill-snapshot-*
*.log
marketplace/
venv/
__pycache__/
*.pyc
"""


def written_patterns(eh: Path) -> list:
    return [ln for ln in (eh / ".gitignore").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("!")]


# ---- A1-A4 ------------------------------------------------------------------------------------------

def test_a1_absent_gitignore_gets_exactly_the_constant_in_order(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    r = run_script(SCRIPT, home, eh)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert written_patterns(eh) == PATTERNS


def test_a2_the_old_14_line_literal_is_brought_up_to_the_constant(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    (eh / ".gitignore").write_text(OLD_LITERAL, encoding="utf-8")
    r = run_script(SCRIPT, home, eh)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert written_patterns(eh) == PATTERNS
    assert len(PATTERNS) > len(OLD_LITERAL.splitlines())  # not vacuous: the fixture really was incomplete


def test_a3_a_complete_gitignore_with_hand_lines_is_left_byte_identical(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    body = "\n".join(PATTERNS) + "\nmy-extra\n!diary/diary.db\n"
    (eh / ".gitignore").write_bytes(body.encode("utf-8"))
    r = run_script(SCRIPT, home, eh)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (eh / ".gitignore").read_bytes() == body.encode("utf-8")


def test_a3b_a_rewrite_keeps_a_hand_negation(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    (eh / ".gitignore").write_text(OLD_LITERAL + "!diary/diary.db\n", encoding="utf-8")
    r = run_script(SCRIPT, home, eh)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "!diary/diary.db" in (eh / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert written_patterns(eh) == PATTERNS


def test_a4_dry_run_writes_nothing_and_names_every_pattern(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    head = seed_git(eh)
    r = run_script(SCRIPT, home, eh, ("--dry-run",))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert not (eh / ".gitignore").exists()
    out_lines = {ln.strip() for ln in r.stdout.splitlines()}
    for p in PATTERNS:
        assert p in out_lines, p
    assert git(eh, "rev-parse", "HEAD").strip() == head


# ---- A5: engram_core.py missing -----------------------------------------------------------------------

def test_a5_missing_engram_core_dies_in_preflight_naming_the_path_and_writes_nothing(sandbox):
    base, home, eh = sandbox
    make_store(eh)
    head = seed_git(eh)
    tree = base / "scratch"
    (tree / "tools").mkdir(parents=True)
    script = tree / "tools" / "engram-fix-git-backup.sh"
    shutil.copy(SCRIPT, script)
    (tree / "engram_backup.py").symlink_to(ENGRAM_BACKUP)  # present; engram_core.py deliberately absent
    r = run_script(script, home, eh)
    assert r.returncode != 0
    assert "engram_core.py" in r.stderr and str(tree) in r.stderr, r.stderr
    assert not (eh / ".gitignore").exists()
    assert not (eh / "knowledge.sql").exists()
    assert git(eh, "rev-parse", "HEAD").strip() == head
    assert not [p for p in home.iterdir() if p.name.startswith(".engram-db-safety-")]  # died before step 1


# ---- A6: the swallowed-OSError post-check ---------------------------------------------------------------

def test_a6_an_unwritable_gitignore_dies_before_step_3(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    head = seed_git(eh)
    (eh / ".gitignore").mkdir()  # _ensure_engram_gitignore hits OSError and returns None
    r = run_script(SCRIPT, home, eh)
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert ".gitignore" in r.stderr
    assert not (eh / "knowledge.sql").exists()
    assert git(eh, "rev-parse", "HEAD").strip() == head  # step 5 never ran
    assert "Step 3" not in r.stdout


# ---- A7: Step 5 and an ignored session_log.md -----------------------------------------------------------

def test_a7_a_home_with_session_log_commits_everything_except_the_ignored_file(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    (eh / "session_log.md").write_text("log\n", encoding="utf-8")
    (eh / "warm-briefing.md").write_text("briefing\n", encoding="utf-8")
    (eh / "config.json").write_text("{}\n", encoding="utf-8")
    (eh / "diary").mkdir()
    (eh / "diary" / "d1.md").write_text("diary\n", encoding="utf-8")
    r = run_script(SCRIPT, home, eh)
    assert r.returncode == 0, (r.stdout, r.stderr)
    git(eh, "rev-parse", "--verify", "HEAD")
    files = git(eh, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "session_log.md" not in files
    for must in (".gitignore", "knowledge.sql", "warm-briefing.md", "config.json", "diary/d1.md"):
        assert must in files, (must, files)
    skipped = [ln for ln in r.stdout.splitlines() if "skipped (gitignored)" in ln]
    assert len(skipped) == 1 and "session_log.md" in skipped[0], r.stdout
    assert (eh / "session_log.md").read_text(encoding="utf-8") == "log\n"  # on disk, untouched


# ---- N1-N3: the claims the script makes about itself (Luria's S1, #4510) --------------------------------

@pytest.mark.parametrize("mode", ["live", "dry"])
def test_n1_a_decoy_engram_core_in_the_cwd_is_never_imported(sandbox, mode):
    _b, home, eh = sandbox
    make_store(eh)
    marker = home / "decoy-engram-core-imported"
    (home / "engram_core.py").write_text(f"open({str(marker)!r}, 'w').write('imported')\n", encoding="utf-8")
    # run_script's cwd is `home`, so under `python -c` sys.path[0] ('') is the decoy's directory
    r = run_script(SCRIPT, home, eh, ("--dry-run",) if mode == "dry" else ())
    assert not marker.exists(), "the cwd's engram_core.py outranked the real module (insert(0) became append?)"
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_n2_an_ignored_file_under_diary_is_skipped_and_the_normal_one_is_committed(sandbox):
    _b, home, eh = sandbox
    make_store(eh)
    (eh / "diary").mkdir()
    (eh / "diary" / "diary.db").write_text("not a db\n", encoding="utf-8")  # ignored by *.db
    (eh / "diary" / "d1.md").write_text("diary\n", encoding="utf-8")
    r = run_script(SCRIPT, home, eh)
    assert r.returncode == 0, (r.stdout, r.stderr)
    files = git(eh, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "diary/d1.md" in files and "diary/diary.db" not in files, files
    skipped = [ln for ln in r.stdout.splitlines() if "skipped (gitignored)" in ln]
    assert len(skipped) == 1 and "diary/diary.db" in skipped[0], r.stdout
    staged = [ln for ln in r.stdout.splitlines() if "Staged:" in ln]
    assert len(staged) == 1 and "diary/diary.db" not in staged[0], r.stdout  # skipped, not counted as staged


def test_n3_a_failing_check_ignore_dies_loudly_and_commits_nothing(sandbox):
    base, home, eh = sandbox
    make_store(eh)
    real_git = shutil.which("git")
    shim_dir = base / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = check-ignore ] && exit 128; done\n'
        f'exec {real_git} "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    r = run_script(SCRIPT, home, eh, env_extra={"PATH": str(shim_dir) + os.pathsep + os.environ["PATH"]})
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "git check-ignore failed" in r.stderr, r.stderr
    assert "Clean root commit" not in r.stdout
    head = subprocess.run([real_git, "-C", str(eh), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": str(eh), "GIT_CONFIG_GLOBAL": os.devnull,
                               "GIT_CONFIG_NOSYSTEM": "1"})
    assert head.returncode != 0, head.stdout  # no commit exists


# ---- B: no second literal copy of the list -------------------------------------------------------------

MIN_COPY_ENTRIES = 10
CANONICAL_REL = "src/engram/engram_core.py"
SCAN_DIRS = ("src", "tools")
SKIP_PARTS = {"__pycache__", ".git", "node_modules"}


def _hits(text: str) -> set:
    lines = {ln.strip() for ln in text.splitlines()}
    found = set()
    for entry in PATTERNS:
        if entry in lines or re.search(r"""(['"])""" + re.escape(entry) + r"\1", text):
            found.add(entry)
    return found


def find_second_copies(root: Path) -> list:
    """Relative paths of files under root/src and root/tools (other than the canonical module) holding
    >= MIN_COPY_ENTRIES canonical entries as whole lines or whole quoted strings."""
    flagged = []
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or SKIP_PARTS & set(p.parts):
                continue
            rel = p.relative_to(root).as_posix()
            if rel == CANONICAL_REL:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if len(_hits(text)) >= MIN_COPY_ENTRIES:
                flagged.append(rel)
    return flagged


def test_b_no_second_literal_copy_of_the_gitignore_list_in_src_or_tools():
    assert find_second_copies(REPO_ROOT) == []


def test_b_the_scan_is_not_vacuous_the_canonical_module_itself_would_trip_it():
    assert len(_hits((REPO_ROOT / CANONICAL_REL).read_text(encoding="utf-8"))) >= MIN_COPY_ENTRIES


@pytest.mark.parametrize("spelling", ["lines", "quoted"])
def test_b_a_planted_second_copy_is_flagged_in_a_scratch_tree(tmp_path, spelling):
    # also: the pre-#4506 14-line literal counts as a copy (it is the very drift this guard exists for)
    (tmp_path / "src" / "x").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "innocent.sh").write_text("echo secrets/\n", encoding="utf-8")
    if spelling == "lines":
        planted = "\n".join(PATTERNS) + "\n"
    else:
        planted = "X = [" + ", ".join(f'"{e}"' for e in PATTERNS) + "]\n"
    (tmp_path / "src" / "x" / "copy.sh").write_text(planted, encoding="utf-8")
    assert find_second_copies(tmp_path) == ["src/x/copy.sh"]


def test_b_the_old_14_line_literal_counts_as_a_copy(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "old.sh").write_text(OLD_LITERAL, encoding="utf-8")
    assert find_second_copies(tmp_path) == ["tools/old.sh"]


def test_b_a_file_with_nine_entries_is_not_a_copy(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "nine.sh").write_text("\n".join(PATTERNS[:MIN_COPY_ENTRIES - 1]) + "\n", encoding="utf-8")
    assert find_second_copies(tmp_path) == []
