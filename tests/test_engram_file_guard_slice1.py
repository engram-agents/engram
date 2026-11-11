"""Unit tests for src/engram/engram_file_guard.py, the one place that decides, backs up and
writes a protected file (see tests/test_never_clobber_contract.py for the end-to-end contract).

Also pins `bootstrap._FIXED_SEED_IDS` against what seeding really mints, because the
check-only pass predicts every render from those IDs without a seed manifest.
"""

from __future__ import annotations

import json
import sys
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
