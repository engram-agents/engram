"""Contract tests for alpha #3919 M6: the dream-to-awake pickup (write half).

Written before the tool and before the two text edits.  The tool
(src/engram/tools/dream_pickup.py) records, on the latest dream record, what the awake seat
did with each head item.  Fixtures carry no real node ids (qux-001 style).
"""

import importlib.util
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
TOOL = SRC / "tools" / "dream_pickup.py"
DREAM_MASTER = SRC / "agents" / "claude" / "engram-dream-master.md"
SLEEP_SKILL = SRC / "skills" / "claude" / "engram-sleep" / "SKILL.md"

RECORD = (
    "# Dream record - 2026-10-19\n\n"
    "**Turn:** 1 -> 2\n\n"
    "## Feelings filed\n- none\n\n"
    "## For the awake seat\n"
    "1. fix qux-001 - one command\n"
    "2. close qux-002 - wrapped over\n   several lines\n"
    "3. re-measure qux-003\n\n"
    "## Open for tomorrow\n- qux-004: a direction\n\n"
    "## Sleep-end triage (parent)\n1. do now - done\n2. defer - later\n3. file (#9) - big\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("dream_pickup_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*args, home=None):
    env = {k: v for k, v in os.environ.items() if k != "ENGRAM_HOME"}
    if home is not None:
        env["ENGRAM_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True, env=env
    )


@pytest.fixture
def rec(tmp_path):
    p = tmp_path / "2026-10-19.md"
    p.write_bytes(RECORD.encode("utf-8"))
    return p


def pickup_lines(path):
    text = path.read_bytes().decode("utf-8")
    if "## Morning pickup" not in text:
        return None
    tail = text.split("## Morning pickup", 1)[1]
    return [ln for ln in tail.splitlines() if ln.strip()]


# --- writes ---------------------------------------------------------------------------------

def test_pick_writes_line_and_section_at_end(rec):
    r = run("--pick", 1, "fixed it", "--record", rec)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"wrote p1 (picked up) to {rec}"
    text = rec.read_text(encoding="utf-8")
    assert text.endswith("## Morning pickup\np1. picked up fixed it\n")
    assert text.index("## Sleep-end triage (parent)") < text.index("## Morning pickup")
    assert text.startswith(RECORD)


def test_skip_writes_line(rec):
    r = run("--skip", 2, "no time today", "--record", rec)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"wrote p2 (skipped) to {rec}"
    assert pickup_lines(rec) == ["p2. skipped (no time today)"]


def test_rerun_replaces_not_duplicates(rec):
    run("--pick", 1, "first", "--record", rec)
    run("--skip", 1, "changed my mind", "--record", rec)
    assert pickup_lines(rec) == ["p1. skipped (changed my mind)"]


def test_sorted_by_n_after_3_then_1(rec):
    run("--pick", 3, "c", "--record", rec)
    run("--pick", 1, "a", "--record", rec)
    run("--skip", 2, "b", "--record", rec)
    assert pickup_lines(rec) == ["p1. picked up a", "p2. skipped (b)", "p3. picked up c"]


def test_different_n_appends(rec):
    run("--pick", 1, "a", "--record", rec)
    run("--pick", 2, "b", "--record", rec)
    assert pickup_lines(rec) == ["p1. picked up a", "p2. picked up b"]


# --- refusals -------------------------------------------------------------------------------

def _refused(r, rec_path=None, before=None):
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert len(r.stderr.strip().splitlines()) == 1
    if rec_path is not None:
        assert rec_path.read_bytes() == before


@pytest.mark.parametrize("n", ["4", "0", "-1", "abc", "1.5", "99"])
def test_bad_n_refused(rec, n):
    before = rec.read_bytes()
    _refused(run("--pick", n, "x", "--record", rec), rec, before)
    _refused(run("--skip", n, "x", "--record", rec), rec, before)


@pytest.mark.parametrize("txt", ["", "   ", "\t \n"])
def test_empty_text_refused(rec, txt):
    before = rec.read_bytes()
    _refused(run("--pick", 1, txt, "--record", rec), rec, before)
    _refused(run("--skip", 1, txt, "--record", rec), rec, before)


def test_no_awake_section_refused(tmp_path):
    p = tmp_path / "r.md"
    p.write_bytes(b"# Dream record\n\n## Open for tomorrow\n1. x\n")
    before = p.read_bytes()
    _refused(run("--pick", 1, "x", "--record", p), p, before)


def test_no_record_at_all_refused(tmp_path):
    (tmp_path / "history" / "dream").mkdir(parents=True)
    _refused(run("--pick", 1, "x", "--home", tmp_path))
    _refused(run("--pick", 1, "x", home=tmp_path / "nope"))


def test_missing_record_file_refused(tmp_path):
    _refused(run("--pick", 1, "x", "--record", tmp_path / "missing.md"))


def test_directory_target_refused(tmp_path):
    d = tmp_path / "dir.md"
    d.mkdir()
    _refused(run("--pick", 1, "x", "--record", d))
    assert list(d.iterdir()) == []


# --- parse ----------------------------------------------------------------------------------

def test_heading_with_parenthetical_suffix(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# T\n\n## For the awake seat (carried)\n1. a qux-001\n", encoding="utf-8")
    assert run("--pick", 1, "ok", "--record", p).returncode == 0
    assert pickup_lines(p) == ["p1. picked up ok"]


def test_wrapped_items_do_not_make_phantom_numbers(rec):
    # '   several lines' is a continuation of item 2, not an item
    assert run("--pick", 2, "ok", "--record", rec).returncode == 0
    assert pickup_lines(rec) == ["p2. picked up ok"]


def test_fenced_block_ignored(tmp_path):
    p = tmp_path / "r.md"
    p.write_text(
        "# T\n\n```markdown\n## For the awake seat\n1. fake\n2. fake\n```\n\n"
        "## For the awake seat\n1. real\n",
        encoding="utf-8",
    )
    assert run("--pick", 1, "ok", "--record", p).returncode == 0
    before = p.read_bytes()
    _refused(run("--pick", 2, "x", "--record", p), p, before)  # 2 only exists inside the fence
    q = tmp_path / "q.md"
    q.write_text("# T\n\n```\n## For the awake seat\n1. fake\n```\n", encoding="utf-8")
    before = q.read_bytes()
    _refused(run("--pick", 1, "x", "--record", q), q, before)


def test_fenced_morning_pickup_heading_ignored(tmp_path):
    p = tmp_path / "r.md"
    p.write_text(
        "# T\n\n## For the awake seat\n1. a\n\n```\n## Morning pickup\np1. picked up fake\n```\n",
        encoding="utf-8",
    )
    assert run("--pick", 1, "real", "--record", p).returncode == 0
    text = p.read_text(encoding="utf-8")
    assert text.endswith("```\n\n## Morning pickup\np1. picked up real\n")
    assert "p1. picked up fake" in text


def test_crlf_preserved(tmp_path):
    p = tmp_path / "r.md"
    p.write_bytes(RECORD.replace("\n", "\r\n").encode("utf-8"))
    assert run("--pick", 1, "ok", "--record", p).returncode == 0
    data = p.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")
    assert data.endswith(b"## Morning pickup\r\np1. picked up ok\r\n")
    assert run("--skip", 2, "r", "--record", p).returncode == 0
    data = p.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")
    assert data.endswith(b"p1. picked up ok\r\np2. skipped (r)\r\n")


def test_no_trailing_newline(tmp_path):
    p = tmp_path / "r.md"
    p.write_bytes(RECORD.rstrip("\n").encode("utf-8"))
    assert run("--pick", 1, "ok", "--record", p).returncode == 0
    text = p.read_text(encoding="utf-8")
    assert text.startswith(RECORD.rstrip("\n") + "\n")
    assert text.endswith("## Morning pickup\np1. picked up ok\n")
    # and an existing last-line pickup without a trailing newline
    q = tmp_path / "q.md"
    q.write_bytes((RECORD + "\n## Morning pickup\np1. picked up a").encode("utf-8"))
    assert run("--pick", 2, "b", "--record", q).returncode == 0
    assert pickup_lines(q) == ["p1. picked up a", "p2. picked up b"]


def test_second_morning_pickup_heading_the_last_is_edited(tmp_path):
    """Review of PR 3953: the skill says the FINAL section; the tool used to edit the first."""
    p = tmp_path / "r.md"
    p.write_text(
        RECORD + "\n## Morning pickup\np1. picked up old\n\n## Morning pickup\np3. skipped (dup)\n",
        encoding="utf-8",
    )
    assert run("--pick", 2, "new", "--record", p).returncode == 0
    text = p.read_text(encoding="utf-8")
    first, second = text.split("## Morning pickup")[1:3]
    assert "p2." not in first and "p1. picked up old" in first
    assert "p2. picked up new" in second and "p3. skipped (dup)" in second


def test_unicode_and_400_char_cap_one_line(rec):
    assert run("--pick", 1, "é" * 400, "--record", rec).returncode == 0
    (line,) = pickup_lines(rec)
    assert line == "p1. picked up " + "é" * 300
    assert run("--skip", 2, "漢字 \n\n multi   space\ttext", "--record", rec).returncode == 0
    assert "p2. skipped (漢字 multi space text)" in pickup_lines(rec)


# --- atomicity and mode ---------------------------------------------------------------------

def test_mode_kept(rec):
    os.chmod(rec, 0o640)
    assert run("--pick", 1, "ok", "--record", rec).returncode == 0
    assert stat.S_IMODE(rec.stat().st_mode) == 0o640
    assert run("--pick", 2, "ok", "--record", rec).returncode == 0
    assert stat.S_IMODE(rec.stat().st_mode) == 0o640


def test_failed_replace_leaves_original_and_no_temp(rec, monkeypatch):
    mod = _load()
    before = rec.read_bytes()

    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(mod.os, "replace", boom)
    rc = mod.main(["--pick", "1", "x", "--record", str(rec)])
    assert rc != 0
    assert rec.read_bytes() == before
    # no temp file is left behind (the hidden sidecar lock file is the one allowed extra name)
    assert sorted(p.name for p in rec.parent.iterdir()) == sorted([rec.name, ".dream_pickup.lock"])


# --- resolution -----------------------------------------------------------------------------

def test_home_flag_and_env_pick_lexicographically_last(tmp_path):
    d = tmp_path / "history" / "dream"
    d.mkdir(parents=True)
    (d / "2026-10-18.md").write_text(RECORD, encoding="utf-8")
    (d / "2026-10-20.md").write_text(RECORD, encoding="utf-8")
    (d / "2026-10-19.md").write_text(RECORD, encoding="utf-8")
    os.utime(d / "2026-10-20.md", (1, 1))  # mtime must not matter
    assert run("--pick", 1, "ok", "--home", tmp_path).returncode == 0
    assert pickup_lines(d / "2026-10-20.md") == ["p1. picked up ok"]
    assert pickup_lines(d / "2026-10-19.md") is None
    assert run("--skip", 2, "r", home=tmp_path).returncode == 0
    assert "p2. skipped (r)" in pickup_lines(d / "2026-10-20.md")


def test_record_flag_wins(tmp_path, rec):
    d = tmp_path / "home" / "history" / "dream"
    d.mkdir(parents=True)
    (d / "2026-12-31.md").write_text(RECORD, encoding="utf-8")
    assert run("--pick", 1, "ok", "--record", rec, "--home", tmp_path / "home").returncode == 0
    assert pickup_lines(rec) == ["p1. picked up ok"]
    assert pickup_lines(d / "2026-12-31.md") is None


def test_help_says_no_git_commit():
    r = run("--help")
    assert r.returncode == 0
    assert "commit" in r.stdout.lower()


# --- text-edit pins -------------------------------------------------------------------------

def test_dream_master_head_rule_and_quoting_section():
    text = DREAM_MASTER.read_text(encoding="utf-8")
    fmt = text.split("# Output contract — dream record", 1)[1]
    awake = fmt.split("## For the awake seat", 1)[1].split("\n## Open for tomorrow", 1)[0]
    assert "at most 3" in awake
    assert "allowed only when no node exists" in awake
    assert "## Previous morning pickup (quoted)" in fmt
    seg = fmt.split("## Previous morning pickup (quoted)", 1)[1][:1600]
    assert "`none`" in seg and "skipped" in seg and "## Morning pickup" in seg
    # the quoted section sits before the first ## section of the record format
    assert fmt.index("## Previous morning pickup (quoted)") < fmt.index("## Fairy contribution")


def test_sleep_skill_step9_and_step95_pins():
    text = SLEEP_SKILL.read_text(encoding="utf-8")
    s9 = text.split("### Step 9 — Receive", 1)[1].split("### Step 9.5", 1)[0]
    assert "awake-seat items: N" in s9 and "at most 3" in s9 and "heads: N" not in s9
    s95 = text.split("### Step 9.5", 1)[1]
    for needle in ("## Morning pickup", "tools/dream_pickup.py", "p1. picked up", "p1. skipped ("):
        assert needle in s95, needle


def test_new_strings_carry_no_real_ids_or_repo_urls():
    for p in (TOOL, Path(__file__)):
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        assert "engram-agents/" + "engram-alpha" not in t
        assert not re.search(r"\b(?:ob|dv|qu|cj|ax|df|gl|pn|fl|tk|lq)_\d{4}\b", t)


def test_the_dream_masters_completion_checklist_carries_the_cap_and_the_quoted_section():
    """Review of the M6 build: the checklist near the end of the spec still said only 'a numbered
    block', so a master following the checklist alone would skip the cap and the quoted section."""
    text = (Path(__file__).resolve().parent.parent / "src" / "engram" / "agents" / "claude" / "engram-dream-master.md").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("- [ ]")]
    assert any("For the awake seat" in ln and "at most 3" in ln and "node id" in ln for ln in lines)
    assert any("Previous morning pickup (quoted)" in ln and "none" in ln for ln in lines)


# --- review round 1 (PR 3953) -----------------------------------------------------------------

def test_a_pickup_heading_copied_to_the_top_before_the_awake_seat_block_is_never_the_target(tmp_path):
    """The dream master quotes the previous pickup at the TOP of tonight's record; if it copies the
    heading too, the tool must still create/edit tonight's section after the awake-seat block."""
    p = tmp_path / "2026-10-19.md"
    p.write_text(
        "# Dream record\n\n## Morning pickup\np1. skipped (quoted from last night)\n\n"
        "## For the awake seat\n1. head one qux-001\n2. head two qux-002\n",
        encoding="utf-8",
    )
    assert run("--pick", 2, "did it", "--record", p).returncode == 0
    text = p.read_text(encoding="utf-8")
    quoted, rest = text.split("## For the awake seat")
    assert "p2." not in quoted and "p1. skipped (quoted from last night)" in quoted
    assert rest.rstrip().endswith("p2. picked up did it") and "## Morning pickup" in rest


def test_parallel_picks_do_not_lose_updates(tmp_path):
    """Review of PR 3953: three parallel runs lost lines in 29 of 30 rounds (read-modify-write with
    no lock, last writer wins, every process exit 0)."""
    for rnd in range(6):
        p = tmp_path / f"r{rnd}.md"
        p.write_text("# R\n\n## For the awake seat\n1. a qux-001\n2. b qux-002\n3. c qux-003\n", encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, str(TOOL), "--pick", str(n), f"did {n}", "--record", str(p)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for n in (1, 2, 3)]
        assert [pr.wait() for pr in procs] == [0, 0, 0]
        text = p.read_text(encoding="utf-8")
        assert all(f"p{n}. picked up did {n}" in text for n in (1, 2, 3)), (rnd, text)


def test_a_symlinked_record_keeps_its_link_and_the_target_is_rewritten(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("# R\n\n## For the awake seat\n1. a qux-001\n", encoding="utf-8")
    link = tmp_path / "link.md"
    os.symlink(real, link)
    assert run("--pick", 1, "x", "--record", link).returncode == 0
    assert link.is_symlink() and "p1. picked up x" in real.read_text(encoding="utf-8")


def test_control_characters_never_reach_the_record(rec):
    assert run("--pick", 1, "bad\x1b[31mred\x07 text", "--record", rec).returncode == 0
    text = rec.read_text(encoding="utf-8")
    assert "\x1b" not in text and "\x07" not in text and "p1. picked up bad [31mred text" in text


def test_the_texts_name_the_right_invocation_and_the_quoting_rule():
    root = Path(__file__).resolve().parent.parent / "src" / "engram"
    skill = (root / "skills" / "claude" / "engram-sleep" / "SKILL.md").read_text(encoding="utf-8")
    master = (root / "agents" / "claude" / "engram-dream-master.md").read_text(encoding="utf-8")
    assert '/tools/dream_pickup.py"' in skill and '"${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}' in skill and "quote the text" in skill
    assert "last `## Morning pickup` section" in skill
    assert "WITHOUT its `## Morning pickup` heading" in master and "WITHOUT the `.md` extension" in master


def test_a_stuck_lock_holder_times_out_with_exit_1_not_a_hang(tmp_path):
    """Review of the delta (PR 3953): no timeout meant a stuck holder stalled every later pick forever."""
    import fcntl
    p = tmp_path / "r.md"
    p.write_text("# R\n\n## For the awake seat\n1. a qux-001\n", encoding="utf-8")
    mod = _load()
    mod._LOCK_WAIT_S = 0.3
    before = p.read_bytes()
    with open(tmp_path / ".dream_pickup.lock", "a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        rc = mod.main(["--pick", "1", "x", "--record", str(p)])
    assert rc == 1 and p.read_bytes() == before


def test_the_lock_sits_beside_the_real_file_for_a_symlinked_record(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "r.md"
    real.write_text("# R\n\n## For the awake seat\n1. a qux-001\n", encoding="utf-8")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link = link_dir / "l.md"
    os.symlink(real, link)
    assert run("--pick", 1, "x", "--record", link).returncode == 0
    assert (real_dir / ".dream_pickup.lock").exists() and not (link_dir / ".dream_pickup.lock").exists()


def test_the_module_docstring_describes_the_last_heading_the_lock_and_the_timeout():
    doc = (Path(TOOL)).read_text(encoding="utf-8").split('"""')[1]
    assert "the LAST one after the awake-seat block" in doc and "FIRST if there are several" not in doc
    assert "flock" in doc and "lock busy" in doc


def test_a_second_dream_on_a_day_is_later_than_the_plain_one(tmp_path):
    """Review of PR 3953 (colleague): `-` sorts before `.`, so plain filename order picked
    2026-10-02.md over the later 2026-10-02-turn110.md, silently, exit 0."""
    d = tmp_path / "history" / "dream"
    d.mkdir(parents=True)
    body = "# R\n\n## For the awake seat\n1. a qux-001\n"
    (d / "2026-10-02.md").write_text(body, encoding="utf-8")
    (d / "2026-10-02-turn110.md").write_text(body, encoding="utf-8")
    r = run("--pick", 1, "did it", "--home", tmp_path)
    assert r.returncode == 0 and "2026-10-02-turn110.md" in r.stdout
    assert "p1." not in (d / "2026-10-02.md").read_text(encoding="utf-8")


def test_a_non_date_md_in_the_folder_is_never_the_latest(tmp_path):
    d = tmp_path / "history" / "dream"
    d.mkdir(parents=True)
    body = "# R\n\n## For the awake seat\n1. a qux-001\n"
    (d / "2026-10-02.md").write_text(body, encoding="utf-8")
    (d / "README.md").write_text(body, encoding="utf-8")
    (d / "notes.md").write_text(body, encoding="utf-8")
    r = run("--pick", 1, "x", "--home", tmp_path)
    assert r.returncode == 0 and "2026-10-02.md" in r.stdout


def test_only_non_date_files_means_no_record(tmp_path):
    d = tmp_path / "history" / "dream"
    d.mkdir(parents=True)
    (d / "README.md").write_text("# x\n", encoding="utf-8")
    assert run("--pick", 1, "x", "--home", tmp_path).returncode == 2


def test_the_texts_carry_the_stem_sort_rule_the_id_slot_and_the_trigger():
    root = Path(__file__).resolve().parent.parent / "src" / "engram"
    skill = (root / "skills" / "claude" / "engram-sleep" / "SKILL.md").read_text(encoding="utf-8")
    master = (root / "agents" / "claude" / "engram-dream-master.md").read_text(encoding="utf-8")
    assert "WITHOUT the `.md` extension" in master and "2026-10-02-turn110.md` is later than `2026-10-02.md" in master
    assert "1. <node id, or a file ref only when no node exists>" in master
    assert "Morning Routine Board item" in skill and "heads: N" not in skill
