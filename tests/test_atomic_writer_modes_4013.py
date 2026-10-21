"""Atomic writers under ENGRAM_HOME keep what a reader through group/ACL needs [#4013].

Contract (written before the implementation; the cells below are the design):

  W1  An existing file keeps its permission bits across a write, and across a
      SECOND write (the bug resets 0660 -> 0600 on every write).
  W2  Under a directory with a default ACL, an existing 0660 file keeps its
      group read/write (os.stat group bits) after a write: a bare
      mkstemp+os.replace leaves group::--- and revokes a named reader.
  W3  A NEW file is created the way a plain open() creates it in the same
      directory (umask / default ACL decide), not forced to 0600.
  W4  A failed write leaves the target byte-identical and leaves no temp file.
  W5  Symlinks (changed after the colleague round, #4024): the reference
      write_config_json and the core with follow_symlinks=True (its default)
      write THROUGH a link, as write_text did. Every other delegating caller
      (the hooks writer, the loop marker writer; walguard, the session-start
      hook's registry and config writes) keeps its pre-#4013 behaviour: the
      link is REPLACED by a regular file, the link's old target is left
      untouched, and the new file takes the new-file mode, never the target's
      mode or group (a planted link in a shared directory must not redirect a
      write or leak a victim file's mode).
  G1  A literal text scan, nothing smarter: every file under src/ and tools/
      whose text contains BOTH the literal calls `mkstemp(` and `os.replace(` is
      named in exactly one of the three dicts below, with a reason.
      It does NOT catch: an aliased mkstemp import (`from tempfile import
      mkstemp as mk`), os.rename, shutil.move, Path.replace,
      NamedTemporaryFile(delete=False), or a writer with no mkstemp at all.
      Widening the scan is left open (#4013).

W1-W5 run against the reference (engram_config_io.write_config_json, already
conforming: the non-vacuity control) and against the two writers this issue
generalises onto it: _hooklib.atomic_write_json and loop_prompt's marker
writer. G1 is red until the plain writers delegate to the shared one.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC_ENGRAM = REPO / "src" / "engram"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def writers():
    sys.path.insert(0, str(SRC_ENGRAM))
    try:
        import engram_config_io
    finally:
        sys.path.remove(str(SRC_ENGRAM))
    hooklib = _load("_hooklib_4013", SRC_ENGRAM / "hooks" / "claude" / "_hooklib.py")
    loop_prompt = _load("loop_prompt_4013", SRC_ENGRAM / "tools" / "loop_prompt.py")

    def via_config(path, payload=None):
        engram_config_io.write_config_json(path, payload if payload is not None else {"k": 1})

    def via_hooklib(path, payload=None):
        hooklib.atomic_write_json(path, payload if payload is not None else {"k": 1})

    def via_marker(path, payload=None):
        marker = payload if payload is not None else {"kind": "heartbeat"}
        loop_prompt._write_next_due_at(str(path), marker, "2026-10-21T16:00:00Z")

    return {
        "config_io (reference)": via_config,
        "_hooklib.atomic_write_json": via_hooklib,
        "loop_prompt marker": via_marker,
    }


WRITER_IDS = ["config_io (reference)", "_hooklib.atomic_write_json", "loop_prompt marker"]


def _mode(path):
    return os.stat(path).st_mode & 0o777


def _default_acl_dir(tmp_path, name="acl"):
    """A directory carrying default:group::rw- (what the install's data dir has).
    Skips the test when this filesystem cannot hold ACLs."""
    d = tmp_path / name
    d.mkdir()
    if shutil.which("setfacl") is None:
        pytest.skip("setfacl not installed")
    r = subprocess.run(["setfacl", "-d", "-m", "g::rw", str(d)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"filesystem does not support default ACLs: {r.stderr.strip()}")
    return d


@pytest.mark.parametrize("wid", WRITER_IDS)
def test_w1_existing_mode_survives_two_writes(writers, tmp_path, wid):
    p = tmp_path / "state.json"
    p.write_text("{}")
    os.chmod(p, 0o660)
    writers[wid](p)
    assert _mode(p) == 0o660, f"first write: {oct(_mode(p))}"
    writers[wid](p)
    assert _mode(p) == 0o660, f"second write: {oct(_mode(p))}"


@pytest.mark.parametrize("wid", WRITER_IDS)
def test_w2_default_acl_group_entry_survives(writers, tmp_path, wid):
    d = _default_acl_dir(tmp_path)
    p = d / "state.json"
    p.write_text("{}")
    os.chmod(p, 0o660)
    assert _mode(p) & 0o060 == 0o060, "setup: the control file must start group-rw"
    writers[wid](p)
    assert _mode(p) & 0o060 == 0o060, f"group rw lost after write: {oct(_mode(p))}"


@pytest.mark.parametrize("wid", WRITER_IDS)
def test_w3_new_file_is_created_like_a_plain_open(writers, tmp_path, wid):
    d = _default_acl_dir(tmp_path, "newfile")
    control = d / "control.json"
    with open(control, "w") as fh:
        fh.write("{}")
    p = d / "state.json"
    writers[wid](p)
    assert _mode(p) == _mode(control), (
        f"new file {oct(_mode(p))} but a plain open() gave {oct(_mode(control))}"
    )


@pytest.mark.parametrize("wid", WRITER_IDS)
def test_w4_failed_write_leaves_target_and_directory_untouched(writers, tmp_path, wid):
    p = tmp_path / "state.json"
    p.write_text('{"keep": true}')
    before_bytes = p.read_bytes()
    before_listing = sorted(os.listdir(tmp_path))
    with pytest.raises(Exception):
        writers[wid](p, {"bad": object()})  # not JSON-serializable
    assert p.read_bytes() == before_bytes
    assert sorted(os.listdir(tmp_path)) == before_listing, "a temp file was left behind"


def test_w5a_the_reference_writes_through_a_symlink(writers, tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    writers["config_io (reference)"](link, {"k": 2})
    assert link.is_symlink(), "the reference replaced the link"
    assert json.loads(real.read_text()) == {"k": 2}


@pytest.mark.parametrize("wid", ["_hooklib.atomic_write_json", "loop_prompt marker"])
def test_w5b_hook_and_tool_writers_replace_a_symlink_and_leave_its_target(writers, tmp_path, wid):
    real = tmp_path / "real.json"
    real.write_text('{"secret": true}')
    os.chmod(real, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    old_umask = os.umask(0o022)
    try:
        control = tmp_path / "control.json"
        with open(control, "w") as fh:
            fh.write("{}")
        writers[wid](link, {"kind": "heartbeat"} if wid == "loop_prompt marker" else {"k": 2})
    finally:
        os.umask(old_umask)
    assert not link.is_symlink(), "the link was written through instead of replaced"
    assert real.read_text() == '{"secret": true}', "the link's old target was changed"
    assert _mode(link) == _mode(control), (
        f"the file that replaced the link has {oct(_mode(link))}; a plain open() gives "
        f"{oct(_mode(control))}, the old target had 0o600 (its mode must not carry over)"
    )


@pytest.fixture(scope="module")
def core():
    sys.path.insert(0, str(SRC_ENGRAM))
    try:
        import engram_config_io
    finally:
        sys.path.remove(str(SRC_ENGRAM))
    return engram_config_io


@pytest.fixture(scope="module")
def hooklib_mod():
    return _load("_hooklib_4013_w5c", SRC_ENGRAM / "hooks" / "claude" / "_hooklib.py")


@pytest.mark.parametrize("which", ["core", "hooks"])
@pytest.mark.parametrize("follow", [True, False])
def test_w5c_atomic_write_text_follow_symlinks_option(core, hooklib_mod, tmp_path, which, follow):
    """The shared writer takes follow_symlinks (default True). True writes through
    the link; False replaces the link, leaves its target alone, and gives the new
    file the new-file mode, not the target's."""
    mod = core if which == "core" else hooklib_mod
    real = tmp_path / "real.txt"
    real.write_text("old")
    os.chmod(real, 0o600)
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    old_umask = os.umask(0o022)
    try:
        control = tmp_path / "control.txt"
        with open(control, "w") as fh:
            fh.write("x")
        mod.atomic_write_text(link, "new", follow_symlinks=follow)
    finally:
        os.umask(old_umask)
    if follow:
        assert link.is_symlink() and real.read_text() == "new"
    else:
        assert not link.is_symlink() and link.read_text() == "new"
        assert real.read_text() == "old", "the link's old target was changed"
        assert _mode(link) == _mode(control)


@pytest.mark.parametrize("which", ["core", "hooks"])
def test_w5d_default_is_to_follow(core, hooklib_mod, tmp_path, which):
    """Not passing follow_symlinks keeps the reference behaviour (write_config_json
    relies on it): the link stays and its target is updated."""
    mod = core if which == "core" else hooklib_mod
    real = tmp_path / "real.txt"
    real.write_text("old")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    mod.atomic_write_text(link, "new")
    assert link.is_symlink() and real.read_text() == "new"


def test_w5e_a_dangling_symlink_is_replaced_not_followed_when_not_following(core, hooklib_mod, tmp_path):
    """A planted link to a path that does not exist must not create that path."""
    for i, mod in enumerate((core, hooklib_mod)):
        victim = tmp_path / f"victim{i}.json"
        link = tmp_path / f"dangling{i}.json"
        link.symlink_to(victim)
        mod.atomic_write_text(link, "x", follow_symlinks=False)
        assert not link.is_symlink() and link.read_text() == "x"
        assert not victim.exists(), "a dangling link target was created"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("which", ["core", "hooks"])
def test_w5f_not_following_never_touches_the_targets_directory(core, hooklib_mod, tmp_path, which):
    """A link whose target sits in a directory the writer cannot write: with
    follow_symlinks=False the write still succeeds (it replaces the link in ITS
    directory) and the target directory is never touched, not even for a temp file."""
    mod = core if which == "core" else hooklib_mod
    ro = tmp_path / "ro"
    ro.mkdir()
    victim = ro / "victim.txt"
    victim.write_text("keep")
    os.chmod(ro, 0o500)
    try:
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        link = dest_dir / "link.txt"
        link.symlink_to(victim)
        mod.atomic_write_text(link, "new", follow_symlinks=False)
        assert not link.is_symlink() and link.read_text() == "new"
        assert victim.read_text() == "keep"
        assert sorted(os.listdir(ro)) == ["victim.txt"], "something was created in the target's directory"
    finally:
        os.chmod(ro, 0o700)


@pytest.mark.parametrize("which", ["core", "hooks"])
@pytest.mark.parametrize("umask", [0o022, 0o002])
@pytest.mark.parametrize("mode", [0o600, 0o640])
@pytest.mark.parametrize("follow", [True, False])
def test_w6_the_staging_file_of_an_existing_file_is_never_group_or_world_accessible(
    core, hooklib_mod, tmp_path, monkeypatch, which, umask, mode, follow
):
    """Found by the security read on #4024: the temp was created 0o666 (so umask
    decided, 0644 or 0664) and the FULL replacement payload was written into it
    before the old mode was restored, so a private file's new contents were
    briefly readable (and, under umask 002, group-writable) in a traversable
    directory. The old mkstemp code staged at 0600. The intended 'new file gets
    what a plain open() gets' policy is for files that do not exist yet; an
    EXISTING file's staging copy must start private. Observed at fsync, which
    runs after the payload is written and flushed and before the mode is restored."""
    mod = core if which == "core" else hooklib_mod
    p = tmp_path / "state.json"
    p.write_text("old")
    os.chmod(p, mode)
    seen = []
    real_fsync = os.fsync

    def spy(fd):
        seen.append(os.fstat(fd).st_mode & 0o777)
        return real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", spy)
    old_umask = os.umask(umask)
    try:
        mod.atomic_write_text(p, "secret replacement payload", follow_symlinks=follow)
    finally:
        os.umask(old_umask)
    assert seen, "fsync was never reached, so the staging mode was not observed"
    assert all(m & 0o077 == 0 for m in seen), (
        f"the staging file had mode(s) {[oct(m) for m in seen]} while holding the payload"
    )
    assert _mode(p) == mode, f"the final mode changed: {oct(_mode(p))}"
    assert p.read_text() == "secret replacement payload"


@pytest.mark.parametrize("which", ["core", "hooks"])
def test_w6b_staging_stays_private_when_following_a_symlink_to_an_existing_file(
    core, hooklib_mod, tmp_path, monkeypatch, which
):
    """Same window through the follow-symlinks path (write_config_json's case)."""
    mod = core if which == "core" else hooklib_mod
    real = tmp_path / "real.json"
    real.write_text("old")
    os.chmod(real, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    seen = []
    real_fsync = os.fsync

    def spy(fd):
        seen.append(os.fstat(fd).st_mode & 0o777)
        return real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", spy)
    old_umask = os.umask(0o022)
    try:
        mod.atomic_write_text(link, "secret", follow_symlinks=True)
    finally:
        os.umask(old_umask)
    assert seen and all(m & 0o077 == 0 for m in seen), [oct(m) for m in seen]
    assert link.is_symlink() and _mode(real) == 0o600


def _creation_modes(mod, monkeypatch, path, **kw):
    """Run atomic_write_text with umask 0 and return the mode of every file the
    writer creates, read from the descriptor IMMEDIATELY after os.open returns
    (so a later chmod cannot hide a broad creation mode)."""
    created = []
    real_open = os.open

    def spy(p, flags, mode=0o777, *a, **k):
        fd = real_open(p, flags, mode, *a, **k)
        if flags & os.O_CREAT:
            created.append(os.fstat(fd).st_mode & 0o777)
        return fd

    monkeypatch.setattr(mod.os, "open", spy)
    old_umask = os.umask(0)
    try:
        mod.atomic_write_text(path, "payload", **kw)
    finally:
        os.umask(old_umask)
    return created


@pytest.mark.parametrize("which", ["core", "hooks"])
@pytest.mark.parametrize("mode", [0o600, 0o640])
@pytest.mark.parametrize("follow", [True, False])
def test_w6c_an_existing_files_temp_is_CREATED_private_not_chmodded_later(
    core, hooklib_mod, tmp_path, monkeypatch, which, mode, follow
):
    """Found by the security read (second finding on c4cd0edf): creating the temp
    broad and chmodding it private before any payload is written is NOT
    equivalent. A reader can open the empty broad temp, keep the descriptor, and
    read the later payload after the chmod (chmod does not revoke an acquired
    descriptor). So the creation mode itself is part of the contract."""
    mod = core if which == "core" else hooklib_mod
    p = tmp_path / "state.json"
    p.write_text("old")
    os.chmod(p, mode)
    created = _creation_modes(mod, monkeypatch, p, follow_symlinks=follow)
    assert created, "no file was created through os.open"
    assert all(m & 0o077 == 0 for m in created), (
        f"the temp was created with mode(s) {[oct(m) for m in created]} (umask 0)"
    )


@pytest.mark.parametrize("which", ["core", "hooks"])
def test_w6d_a_new_file_and_a_replaced_link_keep_the_plain_open_creation_mode(
    core, hooklib_mod, tmp_path, monkeypatch, which
):
    """The other half: the private staging is for EXISTING files only. A new file
    and a replaced symlink are created with the plain-open() policy (0o666 under
    umask 0), so the umask / default ACL still decide."""
    mod = core if which == "core" else hooklib_mod
    fresh = _creation_modes(mod, monkeypatch, tmp_path / "new.json")
    assert fresh == [0o666], [oct(m) for m in fresh]
    real = tmp_path / "real.json"
    real.write_text("x")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    monkeypatch.undo()
    replaced = _creation_modes(mod, monkeypatch, link, follow_symlinks=False)
    assert replaced == [0o666], [oct(m) for m in replaced]


@pytest.mark.parametrize("umask", [0o022, 0o002])
def test_w6e_the_real_hooks_json_helper_stages_and_creates_private_for_an_existing_file(
    hooklib_mod, tmp_path, monkeypatch, umask
):
    """Production hook state goes through atomic_write_json, whose default is
    follow_symlinks=False: the path the earlier staging cells did not exercise
    (they called atomic_write_text with its follow default). Both axes, at
    creation (fstat right after os.open) and at fsync (payload already written)."""
    p = tmp_path / "state.json"
    p.write_text("{}")
    os.chmod(p, 0o600)
    created, at_fsync = [], []
    real_open, real_fsync = os.open, os.fsync

    def spy_open(path, flags, mode=0o777, *a, **k):
        fd = real_open(path, flags, mode, *a, **k)
        if flags & os.O_CREAT:
            created.append(os.fstat(fd).st_mode & 0o777)
        return fd

    def spy_fsync(fd):
        at_fsync.append(os.fstat(fd).st_mode & 0o777)
        return real_fsync(fd)

    monkeypatch.setattr(hooklib_mod.os, "open", spy_open)
    monkeypatch.setattr(hooklib_mod.os, "fsync", spy_fsync)
    old_umask = os.umask(umask)
    try:
        hooklib_mod.atomic_write_json(p, {"secret": "replacement"})
    finally:
        os.umask(old_umask)
    assert created and all(m & 0o077 == 0 for m in created), [oct(m) for m in created]
    assert at_fsync and all(m & 0o077 == 0 for m in at_fsync), [oct(m) for m in at_fsync]
    assert _mode(p) == 0o600


# ---------------------------------------------------------------------------
# G1: literal-scan guard for mkstemp+os.replace writers (see the docstring for what it misses).
# ---------------------------------------------------------------------------

# Already mode-aware ON PURPOSE (they chmod / copy the old mode / use the
# 0o666 + fchmod recipe). Path -> how.
MODE_AWARE = {
    "src/engram/engram_backup.py": "chmods the temp to the old target's mode (comment names the default-ACL case)",
    "src/engram/engram_home_registry.py": "fchmod 0o644",
    "src/forum/coordination/store_file.py": "fixed 0o644, deliberate",
    "src/engram/tools/set_forum_url.py": "copies the old mode onto the temp",
    "src/engram/tools/dream_pickup.py": "copies the old mode onto the temp",
    "src/engram/tools/engram-pkg/engram-pkg": "explicit chmod 0o644 on both outputs",
    "tools/lesson-pattern-audit.py": "old mode (0o640 new) and best-effort ACL",
}

# Private on purpose: only the owner reads it, and the file says so.
PRIVATE_BY_DESIGN = {
    "tools/probes/mods-recall/novel_terms.py": "per-seat probe state in a 0o700 directory",
    "src/engram/hooks/claude/engram-loop-arm-stop-hook.py": (
        "import-failure FALLBACK only; the normal path delegates to the shared writer"
    ),
}

# Carried with a recorded reading, not unruled. The only in-tree caller of
# engramd_owner.snapshot (src/engram/tools/backup_knowledge_db.py, ~lines
# 433-434) stages the snapshot in a mkdtemp directory chmod 0o700 and deletes
# it, so the file ends 0600 and nobody reads it through group or ACL today.
# The owner op's documented contract does name other readers, so the entry stays
# here as a named, reasoned exception rather than moving to PRIVATE_BY_DESIGN.
PENDING_RULING = {
    "src/engram/engramd_owner.py": "snapshot() stages a DB copy via mkstemp then replaces dest: who reads dest?",
}

_ALL_ALLOWED = {**MODE_AWARE, **PRIVATE_BY_DESIGN, **PENDING_RULING}
_CALL = re.compile(r"\bmkstemp\(")
_REPLACE = re.compile(r"\bos\.replace\(")


def _plain_writer_files():
    found = []
    for root in ("src", "tools"):
        for path in (REPO / root).rglob("*"):
            if not path.is_file() or path.suffix in {".json", ".md", ".png", ".pyc", ".db"}:
                continue
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if _CALL.search(text) and _REPLACE.search(text):
                # tools/ holds symlinks into src/engram/tools/: key by the real
                # path so one file is never counted (or missed) under two names.
                found.append(path.resolve().relative_to(REPO.resolve()).as_posix())
    return sorted(set(found))


def test_g1_every_mkstemp_replace_writer_is_named_with_a_reason():
    unnamed = [p for p in _plain_writer_files() if p not in _ALL_ALLOWED]
    assert not unnamed, (
        "plain tempfile.mkstemp + os.replace writers (they reset a file to 0600 and "
        "drop the ACL group entry on every write); delegate to the shared "
        "mode-preserving writer or name the file in MODE_AWARE / PRIVATE_BY_DESIGN "
        f"with a reason: {unnamed}"
    )


def test_g1_every_allowlist_entry_has_a_reason_and_still_exists():
    for table in (MODE_AWARE, PRIVATE_BY_DESIGN, PENDING_RULING):
        for rel, why in table.items():
            assert why and why.strip(), f"{rel}: empty reason"
            assert (REPO / rel).is_file(), f"{rel}: listed but missing (stale entry)"


def test_g1_allowlist_entries_do_not_overlap():
    seen = {}
    for name, table in (("MODE_AWARE", MODE_AWARE), ("PRIVATE_BY_DESIGN", PRIVATE_BY_DESIGN),
                        ("PENDING_RULING", PENDING_RULING)):
        for rel in table:
            assert rel not in seen, f"{rel} is in both {seen[rel]} and {name}"
            seen[rel] = name
