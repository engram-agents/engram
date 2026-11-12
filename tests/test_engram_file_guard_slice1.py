"""Unit tests for src/engram/engram_file_guard.py, the one place that decides, backs up and
writes a protected file (see tests/test_never_clobber_contract.py for the end-to-end contract).

Also pins `bootstrap._FIXED_SEED_IDS` against what seeding really mints, because the
check-only pass predicts every render from those IDs without a seed manifest.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bootstrap_no_clobber_4440 import SRC, _probe, _run  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("engram_file_guard_under_test", SRC / "engram_file_guard.py")
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)

STAMP = "20261111T000000Z"


def _backups(path: Path) -> list[Path]:
    return sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + ".engram-backup."))


def test_absent_is_written_and_identical_is_left_alone(tmp_path):
    target = tmp_path / "sub" / "a.txt"
    written = fg.apply(fg.plan([fg.Item(target, "new\n")], STAMP))
    assert written == [target] and target.read_text() == "new\n"
    before = target.stat().st_mtime_ns
    again = fg.apply(fg.plan([fg.Item(target, "new\n")], STAMP))
    assert again == [] and target.stat().st_mtime_ns == before
    assert _backups(target) == [] and fg.plan([fg.Item(target, "new\n")]).entries[0].state == "identical"


def test_plan_writes_nothing_and_names_the_backup(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("mine\n")
    p = fg.plan([fg.Item(target, "new\n"), fg.Item(tmp_path / "b.txt", "x")], STAMP)
    assert [e.state for e in p.entries] == ["differs", "absent"]
    assert p.entries[0].backup == tmp_path / f"a.txt.engram-backup.{STAMP}"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["a.txt"]


def test_refusal_names_every_path_without_consent_and_apply_changes_nothing(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.write_text("A")
    b.write_text("B")
    p = fg.plan([fg.Item(a, "1"), fg.Item(b, "2"), fg.Item(c, "3")], STAMP)
    msg = fg.refusal(p)
    assert msg is not None and msg.count("REFUSED") == 1
    assert str(a) in msg and str(b) in msg and str(c) not in msg
    with pytest.raises(fg.Refused):
        fg.apply(p)
    assert (a.read_text(), b.read_text(), c.exists()) == ("A", "B", False), "nothing may change under a refusal"
    one = fg.consent_for(False, [str(a)])
    msg_one = fg.refusal(p, one)
    assert str(b) in msg_one and str(a) not in msg_one, "--replace A consents for A only"
    assert fg.refusal(p, fg.consent_for(True, [])) is None


def test_consented_replace_backs_up_exact_bytes_first(tmp_path):
    a = tmp_path / "a"
    a.write_bytes(b"mine \xff\n")
    fg.apply(fg.plan([fg.Item(a, "new")], STAMP), fg.consent_for(False, [str(a)]))
    assert a.read_text() == "new"
    assert [p.read_bytes() for p in _backups(a)] == [b"mine \xff\n"]


def test_backup_name_collision_takes_a_numbered_suffix(tmp_path):
    a = tmp_path / "a"
    a.write_text("mine")
    (tmp_path / f"a.engram-backup.{STAMP}").write_text("taken")
    fg.apply(fg.plan([fg.Item(a, "new")], STAMP), fg.consent_for(True, []))
    assert (tmp_path / f"a.engram-backup.{STAMP}").read_text() == "taken"
    assert (tmp_path / f"a.engram-backup.{STAMP}-1").read_text() == "mine"


def test_identity_file_keeps_pre_engram_once_and_every_replace_is_stamped(tmp_path):
    a = tmp_path / "CLAUDE.md"
    a.write_text("first")
    fg.apply(fg.plan([fg.Item(a, "n1", first_identity=True)], STAMP), fg.consent_for(True, []))
    assert (tmp_path / "CLAUDE.md.pre-engram").read_text() == "first"
    assert [p.read_text() for p in _backups(a)] == ["first"], "every replace gets a stamped backup too"
    fg.apply(fg.plan([fg.Item(a, "n2", first_identity=True)], STAMP), fg.consent_for(True, []))
    assert (tmp_path / "CLAUDE.md.pre-engram").read_text() == "first", ".pre-engram is never modified"
    assert sorted(p.read_text() for p in _backups(a)) == ["first", "n1"]
    other = tmp_path / "plain.txt"
    other.write_text("x")
    fg.apply(fg.plan([fg.Item(other, "y")], STAMP), fg.consent_for(True, []))
    assert not (tmp_path / "plain.txt.pre-engram").exists(), ".pre-engram is for identity files only"


def test_a_backup_that_cannot_be_written_refuses_that_write(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.write_text("mine")

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fg, "copy_exclusive", boom)
    with pytest.raises(SystemExit) as e:
        fg.apply(fg.plan([fg.Item(a, "new")], STAMP), fg.consent_for(True, []))
    assert "REFUSED" in str(e.value) and str(a) in str(e.value)
    assert a.read_text() == "mine"


def test_dangling_symlink_and_directory_are_never_consentable(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "nowhere")
    d = tmp_path / "dir"
    d.mkdir()
    p = fg.plan([fg.Item(link, "x"), fg.Item(d, "y")], STAMP)
    assert [e.state for e in p.entries] == ["dangling", "blocked"]
    msg = fg.refusal(p, fg.consent_for(True, []))
    assert msg is not None and str(link) in msg and str(d) in msg
    assert not (tmp_path / "nowhere").exists()


def test_a_symlinked_file_is_replaced_through_the_link(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("mine")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    fg.apply(fg.plan([fg.Item(link, "new")], STAMP), fg.consent_for(True, []))
    assert link.is_symlink() and real.read_text() == "new"
    assert [p.read_text() for p in _backups(link)] == ["mine"]


def test_installer_needs_a_yes_even_when_an_earlier_backup_exists(tmp_path):
    """The earlier-backup branch of install.sh step 6 asks the same question as the first-backup
    branch: with no terminal and no --yes it stops and leaves CLAUDE.md alone."""
    from test_install_sh_4097 import Sandbox

    sb = Sandbox(tmp_path)
    sb.claude_home.mkdir(parents=True)
    md = sb.claude_home / "CLAUDE.md"
    md.write_bytes(b"user-authored current file\n")
    (sb.claude_home / "CLAUDE.md.pre-engram").write_bytes(b"user-authored first file\n")
    r = sb.run()
    assert r.returncode != 0 and "FAIL" in r.all, r.all
    assert md.read_bytes() == b"user-authored current file\n"
    assert (sb.claude_home / "CLAUDE.md.pre-engram").read_bytes() == b"user-authored first file\n"
    r2 = sb.run("--yes")
    assert r2.returncode == 0, r2.all
    assert md.read_bytes() != b"user-authored current file\n"


def test_fixed_seed_ids_match_what_seeding_mints(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    minted = json.loads((tmp_path / ".engram" / "seed-manifest.json").read_text())["seed_ids"]
    probe = _probe(tmp_path / "other", "import json; print(json.dumps(bootstrap._FIXED_SEED_IDS))")
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert json.loads(probe.stdout.strip().splitlines()[-1]) == minted
    assert list(minted) == list(json.loads(probe.stdout.strip().splitlines()[-1]))


# ------------------------------------------------------------- C: same-second backup names (-N)

def test_same_second_backups_take_the_dash_one_suffix_and_the_first_stays_byte_identical(tmp_path, monkeypatch):
    """The stamp clock is the module seam `CLOCK`. With it fixed, two replaces in the same second
    collide on the stamped name: plan() predicts the -1 name, apply() uses it, and the first
    stamped backup is byte-for-byte (and mtime-for-mtime) what it was."""
    from datetime import datetime, timezone
    monkeypatch.setattr(fg, "CLOCK", lambda: datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc))
    a = tmp_path / "a"
    a.write_bytes(b"v1\n")
    consent = fg.consent_for(True, [])

    fg.apply(fg.plan([fg.Item(a, "n1\n")]), consent)  # no explicit stamp: the clock supplies it
    first = tmp_path / "a.engram-backup.20261111T120000Z"
    assert first.read_bytes() == b"v1\n"
    first_state = (first.read_bytes(), first.stat().st_mtime_ns, first.stat().st_mode)

    second_plan = fg.plan([fg.Item(a, "n2\n")])
    assert second_plan.entries[0].backup == tmp_path / "a.engram-backup.20261111T120000Z-1", \
        "plan() must predict the numbered name"
    fg.apply(second_plan, consent)
    assert (tmp_path / "a.engram-backup.20261111T120000Z-1").read_bytes() == b"n1\n"
    assert (first.read_bytes(), first.stat().st_mtime_ns, first.stat().st_mode) == first_state, \
        "the first stamped backup was touched by the second one"

    fg.apply(fg.plan([fg.Item(a, "n3\n")]), consent)
    assert (tmp_path / "a.engram-backup.20261111T120000Z-2").read_bytes() == b"n2\n"
    assert a.read_bytes() == b"n3\n"


# ---------------------------------------- E: consent compares resolved paths (C9)

def test_replace_accepts_a_second_spelling_of_the_same_path(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_dir)
    target = real_dir / "CLAUDE.md"
    target.write_text("mine")
    p = fg.plan([fg.Item(target, "new")], STAMP)
    assert fg.refusal(p, fg.consent_for(False, [str(alias / "CLAUDE.md")])) is None, \
        "a symlinked-folder spelling of the same path was not accepted"
    assert fg.refusal(p, fg.consent_for(False, [str(tmp_path / "real" / ".." / "real" / "CLAUDE.md")])) is None, \
        "a dot-dot spelling of the same path was not accepted"
    # and the other way round: the plan holds the alias spelling, consent the real one
    p_alias = fg.plan([fg.Item(alias / "CLAUDE.md", "new")], STAMP)
    assert fg.refusal(p_alias, fg.consent_for(False, [str(target)])) is None
    assert fg.refusal(p, fg.consent_for(False, [str(tmp_path / "real" / "other.md")])) is not None, \
        "consent for a different file must not count"


def test_replace_accepts_a_relative_path_with_the_cwd_set(tmp_path, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    target = d / "a.md"
    target.write_text("mine")
    monkeypatch.chdir(tmp_path)
    p = fg.plan([fg.Item(target, "new")], STAMP)
    assert fg.refusal(p, fg.consent_for(False, ["d/a.md"])) is None
    assert fg.refusal(p, fg.consent_for(False, ["./d/../d/a.md"])) is None


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/var").is_symlink() or not os.access("/private/var/tmp", os.W_OK),
    reason="the /var vs /private/var spelling exists on macOS only, and /private/var/tmp must be writable",
)
def test_replace_accepts_the_var_and_private_var_spellings():
    """The pair is built explicitly under /private/var/tmp (not taken from tmp_path, whose
    location depends on TMPDIR): the same file spelled /private/var/tmp/... and /var/tmp/..."""
    base = Path(tempfile.mkdtemp(dir="/private/var/tmp"))
    try:
        real = base / "a.md"
        real.write_text("mine")
        long_spelling = str(real)
        assert long_spelling.startswith("/private/var/tmp/"), long_spelling
        short = long_spelling[len("/private"):]
        assert short.startswith("/var/tmp/")
        p = fg.plan([fg.Item(real, "new")], STAMP)
        assert fg.refusal(p, fg.consent_for(False, [short])) is None
        assert fg.refusal(fg.plan([fg.Item(Path(short), "new")], STAMP), fg.consent_for(False, [long_spelling])) is None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_symlink_at_the_path_is_not_followed_for_the_decision(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("mine")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    p = fg.plan([fg.Item(link, "new")], STAMP)
    assert fg.refusal(p, fg.consent_for(False, [str(real)])) is not None, \
        "consent for the link's target must not count as consent for the link"
    assert fg.refusal(p, fg.consent_for(False, [str(link)])) is None


def test_refusal_prints_one_canonical_form(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_dir)
    (real_dir / "a.md").write_text("mine")
    msg = fg.refusal(fg.plan([fg.Item(alias / "a.md", "new")], STAMP))
    canonical = Path(os.path.realpath(real_dir)) / "a.md"
    assert str(canonical) in msg and str(alias) not in msg, msg
    assert f"{canonical}.engram-backup.{STAMP}" in msg, "the backup is printed in the same canonical form"


# ------------------------------------------- G: an obstructed `<name>.pre-engram` (C8)

def test_pre_engram_that_is_the_same_file_is_refused_even_with_consent(tmp_path):
    for how in ("hard", "symlink"):
        d = tmp_path / how
        d.mkdir()
        f = d / "CLAUDE.md"
        f.write_text("mine")
        bak = d / "CLAUDE.md.pre-engram"
        os.link(f, bak) if how == "hard" else os.symlink(f, bak)
        p = fg.plan([fg.Item(f, "new", first_identity=True)], STAMP)
        assert p.entries[0].state == "obstructed", how
        msg = fg.refusal(p, fg.consent_for(True, []))
        assert msg is not None and msg.count("REFUSED") == 1 and str(bak) in msg and "same file" in msg
        assert "Move that backup name aside" in msg
        with pytest.raises(fg.Refused):
            fg.apply(p, fg.consent_for(True, []))
        assert f.read_text() == "mine" and _backups(f) == []


def test_pre_engram_dangling_link_or_directory_is_left_alone_and_the_run_proceeds(tmp_path):
    for how in ("dangling", "dir"):
        d = tmp_path / how
        d.mkdir()
        f = d / "CLAUDE.md"
        f.write_text("mine")
        bak = d / "CLAUDE.md.pre-engram"
        bak.symlink_to(d / "nowhere") if how == "dangling" else bak.mkdir()
        p = fg.plan([fg.Item(f, "new", first_identity=True)], STAMP)
        assert p.entries[0].state == "differs", how
        fg.apply(p, fg.consent_for(True, []))
        assert f.read_text() == "new" and [b.read_text() for b in _backups(f)] == ["mine"]
        assert bak.is_symlink() == (how == "dangling") and not (d / "nowhere").exists()


def test_same_file_answers_three_ways(tmp_path):
    """The one predicate: SAME for another name of the file, DIFFERENT for another file or for
    nothing there (missing, or a path through a non-directory), never an exception for those."""
    f = tmp_path / "CLAUDE.md"
    f.write_text("mine")
    os.link(f, tmp_path / "hard")
    (tmp_path / "other").write_text("mine")
    assert fg.same_file(f, tmp_path / "hard") == (fg.SAME, None)
    assert fg.same_file(f, tmp_path / "other") == (fg.DIFFERENT, None)
    assert fg.same_file(f, tmp_path / "missing") == (fg.DIFFERENT, None)
    assert fg.same_file(f, f / "under-a-file") == (fg.DIFFERENT, None)


def test_missing_pre_engram_still_proceeds(tmp_path):
    """The ordinary first install: no backup name yet. A missing name must read as DIFFERENT,
    never as 'cannot tell', or every fresh install would be refused."""
    f = tmp_path / "CLAUDE.md"
    f.write_text("mine")
    p = fg.plan([fg.Item(f, "new", first_identity=True)], STAMP)
    assert p.entries[0].state == "differs"
    fg.apply(p, fg.consent_for(True, []))
    assert f.read_text() == "new"
    assert (tmp_path / "CLAUDE.md.pre-engram").read_text() == "mine"


@pytest.mark.parametrize("code", ["EACCES", "ELOOP", "EIO"])
def test_first_backup_check_that_fails_is_refused_even_with_consent(tmp_path, monkeypatch, code):
    """When the same-file check on `<name>.pre-engram` fails for any reason other than 'nothing
    there', the run is refused with a message naming the path and the error: not a traceback,
    and not a replace that might overwrite the only backup."""
    import errno
    f = tmp_path / "CLAUDE.md"
    f.write_text("mine")
    (tmp_path / "CLAUDE.md.pre-engram").write_text("older")
    num = getattr(errno, code)

    def boom(a, b):
        raise OSError(num, os.strerror(num), str(b))

    monkeypatch.setattr(fg.os.path, "samefile", boom)
    p = fg.plan([fg.Item(f, "new", first_identity=True)], STAMP)
    assert p.entries[0].state == "unreadable", p.entries[0]
    msg = fg.refusal(p, fg.consent_for(True, []))
    assert msg is not None and msg.count("REFUSED") == 1, msg
    assert "could not tell" in msg and os.strerror(num) in msg and "CLAUDE.md.pre-engram" in msg, msg
    with pytest.raises(fg.Refused):
        fg.apply(p, fg.consent_for(True, []))
    assert f.read_text() == "mine" and _backups(f) == []
    assert (tmp_path / "CLAUDE.md.pre-engram").read_text() == "older"


def test_fixed_backup_path_check_that_fails_is_a_clean_refusal(tmp_path, monkeypatch):
    """The fixed-name backup (backup_path_problem / backup_once) uses the same predicate: a
    failed check is a REFUSED line, not an OSError traceback; a missing backup name is fine."""
    import errno
    f = tmp_path / "settings.json"
    f.write_text("{}")
    bak = tmp_path / "settings.json.corrupt.20260101T000000Z"
    assert fg.backup_path_problem(f, bak) is None  # nothing there yet: proceed
    bak.write_text("{}")

    def boom(a, b):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(b))

    monkeypatch.setattr(fg.os.path, "samefile", boom)
    msg = fg.backup_path_problem(f, bak)
    assert msg is not None and msg.startswith("[bootstrap] REFUSED: could not tell"), msg
    assert str(bak) in msg and os.strerror(errno.EACCES) in msg, msg
    with pytest.raises(SystemExit) as got:
        fg.backup_once(f, bak)
    assert "could not tell" in str(got.value)


def test_obstructed_name_is_listed_in_the_same_one_refusal_as_the_other_paths(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("mine")
    os.link(f, tmp_path / "CLAUDE.md.pre-engram")
    other = tmp_path / "AGENTS.md"
    other.write_text("also mine")
    msg = fg.refusal(fg.plan([fg.Item(f, "n", first_identity=True), fg.Item(other, "n")], STAMP))
    assert msg.count("REFUSED") == 1 and str(f) in msg and str(other) in msg, msg


# ------------------------------------------------ I: backups accumulate, said plainly

def test_refusal_says_backups_accumulate_and_nothing_deletes_them(tmp_path):
    a = tmp_path / "a"
    a.write_text("mine")
    msg = fg.refusal(fg.plan([fg.Item(a, "new")], STAMP))
    assert "Backups accumulate" in msg and "nothing deletes them automatically" in msg, msg


# ------------------------------------------------ J: what a backup promises (C7)

def test_backup_keeps_bytes_mode_and_mtime(tmp_path):
    a = tmp_path / "a"
    a.write_bytes(b"mine \x00\xff\n")
    os.chmod(a, 0o640)
    os.utime(a, ns=(1_000_000_000_123_456_789, 1_000_000_000_123_456_789))
    fg.apply(fg.plan([fg.Item(a, "new")], STAMP), fg.consent_for(True, []))
    (backup,) = _backups(a)
    st = backup.stat()
    assert backup.read_bytes() == b"mine \x00\xff\n"
    assert st.st_mode & 0o7777 == 0o640, "the backup lost the file's mode"
    assert st.st_mtime_ns == 1_000_000_000_123_456_789, "the backup lost the file's modification time"
    # Extended attributes and ACLs are NOT promised (shutil.copystat drops them on macOS); the
    # contract and README-AGENT say so, and this test does not claim otherwise.


# ------------------------------------------------ K: apply() backs up EVERY differing path before the first write

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_apply_with_a_second_unbackable_path_replaces_nothing(tmp_path):
    """Two differing, consented files; the second one's directory is read-only, so its backup
    cannot be written. apply() (called without `saved`, as when a path changed between the two
    passes) must refuse BEFORE replacing the first: both files keep their bytes."""
    first = tmp_path / "ok" / "a.md"
    second = tmp_path / "locked" / "b.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("mine A")
    second.write_text("mine B")
    os.chmod(second.parent, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(SystemExit) as e:
            fg.apply(fg.plan([fg.Item(first, "new A"), fg.Item(second, "new B")], STAMP), fg.consent_for(True, []))
    finally:
        os.chmod(second.parent, stat.S_IRWXU)
    assert "REFUSED" in str(e.value) and str(second) in str(e.value)
    assert first.read_text() == "mine A", "the first file was replaced although the second could not be saved"
    assert second.read_text() == "mine B"


def test_apply_backs_up_every_differing_path_before_writing_any(tmp_path, monkeypatch):
    """Order, observed: no write happens until the last backup has been taken."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("mine A")
    b.write_text("mine B")
    events: list[str] = []
    real_copy, real_write = fg.copy_exclusive, fg._write
    monkeypatch.setattr(fg, "copy_exclusive", lambda s, d: (events.append("backup"), real_copy(s, d))[1])
    monkeypatch.setattr(fg, "_write", lambda p, t: (events.append("write"), real_write(p, t))[1])
    fg.apply(fg.plan([fg.Item(a, "n"), fg.Item(b, "n")], STAMP), fg.consent_for(True, []))
    assert events == ["backup", "backup", "write", "write"], events


# ------------------------------------------------ L: ENGRAM's own bookkeeping files are rewritten, no consent

def test_engram_owned_files_are_rewritten_without_consent_or_backup(tmp_path):
    marker = tmp_path / "first-run-pending"
    manifest = tmp_path / "seed-manifest.json"
    marker.write_text('{"created": "an earlier run"}')
    manifest.write_text('{"seed_ids": {}}')
    items = [fg.Item(marker, "new marker", owned=True), fg.Item(manifest, "new manifest", owned=True)]
    p = fg.plan(items, STAMP, home=tmp_path)
    assert [e.state for e in p.entries] == ["owned", "owned"]
    assert fg.refusal(p) is None
    assert sorted(fg.apply(p)) == sorted([marker, manifest])
    assert (marker.read_text(), manifest.read_text()) == ("new marker", "new manifest")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["first-run-pending", "seed-manifest.json"], "no backup expected"


def test_the_owned_list_is_closed_a_caller_cannot_widen_it(tmp_path):
    f = tmp_path / "warm-briefing.md"
    f.write_text("mine")
    p = fg.plan([fg.Item(f, "new", owned=True)], STAMP, home=tmp_path)
    assert p.entries[0].state == "differs", "owned=True is honoured only for the names on the closed list"
    assert fg.refusal(p) is not None
    assert fg.ENGRAM_OWNED_NAMES == ("first-run-pending", "seed-manifest.json")


def test_a_leftover_first_run_pending_alone_does_not_block_bootstrap(tmp_path):
    marker = tmp_path / ".engram" / "first-run-pending"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"created": "2020-01-01T00:00:00Z", "install_path": "/elsewhere"}')
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "elsewhere" not in marker.read_text(), "the marker was not rewritten"
    assert sorted(x.name for x in marker.parent.iterdir() if "engram-backup" in x.name) == []


# ------------------------------------------------ M: smaller consent / reading points

def _case_insensitive_fs(d: Path) -> bool:
    probe = d / "CaseProbe.tmp"
    probe.write_text("x")
    try:
        return (d / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_replace_matches_a_name_that_is_the_same_file_ignoring_case(tmp_path):
    """macOS APFS is case-insensitive by default: `--replace .../claude.md` names CLAUDE.md."""
    if not _case_insensitive_fs(tmp_path):
        pytest.skip("this filesystem is case-sensitive; the lower-case spelling is a different file here")
    f = tmp_path / "CLAUDE.md"
    f.write_text("mine")
    p = fg.plan([fg.Item(f, "new")], STAMP)
    assert fg.refusal(p, fg.consent_for(False, [str(tmp_path / "claude.md")])) is None


def test_replace_does_not_accept_a_hard_link_under_another_name(tmp_path):
    """Same file, different NAME (not a case/Unicode variant): not the file the user was shown."""
    f = tmp_path / "CLAUDE.md"
    f.write_text("mine")
    alias = tmp_path / "alias.md"
    os.link(f, alias)
    p = fg.plan([fg.Item(f, "new")], STAMP)
    assert fg.refusal(p, fg.consent_for(False, [str(alias)])) is not None


def test_replace_does_not_accept_a_case_variant_that_is_a_different_file(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("mine")
    other = tmp_path / "claude.MD"
    other.write_text("another")
    if f.read_text() != "mine":  # a case-insensitive filesystem folded the two names into one file
        pytest.skip("this filesystem folds the two names into one file")
    p = fg.plan([fg.Item(f, "new")], STAMP)
    assert fg.refusal(p, fg.consent_for(False, [str(other)])) is not None


def test_identical_check_reads_utf8_whatever_the_locale(tmp_path):
    """The writes are UTF-8; the check must read UTF-8 too, so a file already holding the new
    non-ASCII text is `identical`, not `differs`, under a C/ASCII locale."""
    f = tmp_path / "a.md"
    body = "café — done\n"
    f.write_bytes(body.encode("utf-8"))
    # The child program goes in an ASCII file (ascii() spells the body with escapes), not on the
    # command line: under LC_ALL=C with UTF-8 mode off, Linux decodes argv with the locale, so a
    # non-ASCII -c string stops the child before classify runs.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib.util, locale\n"
        "from pathlib import Path\n"
        f"s = importlib.util.spec_from_file_location('g', {ascii(str(SRC / 'engram_file_guard.py'))})\n"
        "g = importlib.util.module_from_spec(s); s.loader.exec_module(g)\n"
        "print(locale.getpreferredencoding(False))\n"
        f"print(g.classify(Path({ascii(str(f))}), {ascii(body)}))\n",
        encoding="ascii",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env.update({"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
    r = subprocess.run([sys.executable, str(probe)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    encoding, verdict = r.stdout.split()
    if encoding.lower().replace("-", "") in ("utf8", "utf_8"):
        pytest.skip(f"the C locale here reads as {encoding}, so this run cannot tell a utf-8 read from a locale read")
    assert verdict == "identical", r.stdout + r.stderr


def test_history_or_diary_holding_only_dotfiles_is_not_memory(tmp_path):
    for sub in ("history", "diary"):
        d = tmp_path / ".engram" / sub
        d.mkdir(parents=True)
        (d / ".DS_Store").write_bytes(b"\x00")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / ".engram" / "knowledge.db").exists()


def test_history_with_a_real_file_is_still_memory(tmp_path):
    d = tmp_path / ".engram" / "history"
    d.mkdir(parents=True)
    (d / ".DS_Store").write_bytes(b"\x00")
    (d / "2026-01-01.md").write_text("an entry")
    r = _run(tmp_path)
    assert r.returncode != 0 and "memory" in (r.stdout + r.stderr)
    assert not (tmp_path / ".engram" / "knowledge.db").exists()


# ------------------------------------------------ N: merge refuses what a rewrite would silently change

@pytest.mark.parametrize("text", [
    '{"model": "a", "model": "b"}',
    '{"hooks": {"x": 1, "x": 2}, "theme": "dark"}',
])
def test_merge_refuses_a_duplicate_key_and_leaves_the_file_alone(tmp_path, text):
    path = tmp_path / "settings.json"
    path.write_text(text)
    with pytest.raises(fg.MergeRefused) as e:
        fg.apply_merge(path, {"cleanupPeriodDays": 30})
    assert "more than once" in str(e.value)
    assert path.read_text() == text and sorted(x.name for x in tmp_path.iterdir()) == ["settings.json"]


@pytest.mark.parametrize("text", [
    '{"limit": 1e400}', '{"limit": -1e400}', '{"x": Infinity}', '{"x": NaN}', '{"x": -Infinity}',
])
def test_merge_refuses_a_non_finite_number_and_leaves_the_file_alone(tmp_path, text):
    path = tmp_path / "settings.json"
    path.write_text(text)
    with pytest.raises(fg.MergeRefused):
        fg.apply_merge(path, {"cleanupPeriodDays": 30})
    assert path.read_text() == text and sorted(x.name for x in tmp_path.iterdir()) == ["settings.json"]


def test_merge_says_number_spellings_may_be_normalised_and_does_so_visibly(tmp_path):
    assert "NUMBER SPELLINGS MAY BE NORMALISED" in fg.merge_json_text.__doc__
    path = tmp_path / "settings.json"
    path.write_text('{"n": 1E5}')
    assert fg.apply_merge(path, {"cleanupPeriodDays": 30}) == "merged"
    assert json.loads(path.read_text())["n"] == 100000.0 and "100000.0" in path.read_text()


# ------------------------------------------------ O: atomic_write_text temp names are unique per call

def test_atomic_write_survives_a_leftover_temp_of_the_same_pid(tmp_path):
    target = tmp_path / "a.txt"
    leftover = tmp_path / f".a.txt.tmp-{os.getpid()}"
    leftover.write_text("a crashed run's leftover")
    fg.atomic_write_text(target, "new", 0o600)
    assert target.read_text() == "new"
    assert leftover.read_text() == "a crashed run's leftover", "an unrelated temp was touched"
    assert sorted(x.name for x in tmp_path.iterdir()) == [leftover.name, "a.txt"], "this call left a temp behind"


def test_atomic_write_temp_names_differ_per_call(tmp_path, monkeypatch):
    names: list[str] = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (names.append(os.path.basename(s)), real(s, d))[1])
    target = tmp_path / "a.txt"
    fg.atomic_write_text(target, "1", 0o600)
    fg.atomic_write_text(target, "2", 0o600)
    assert len(names) == 2 and names[0] != names[1], names
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
