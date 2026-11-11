"""#4499: the installer and the three unit templates stop making the forum tree group-writable.

Contract-first (#3045): committed BEFORE the implementation. Contract: Ariadne's spec
4499-forum-modes.md plus its Rulings section.

- I1  after the installer's mode phase, with ``umask 0002`` OR ``0022`` in the calling shell,
      ``find "$SERVICE_DIR" -xdev -perm -g+w ! -type l`` lists nothing. A real install is too
      heavy for a cell (venv, snapshot, systemd), so the phase that sets modes is a function
      block in the installer (``# >>> service-modes`` ... ``# <<< service-modes``) that this
      file extracts and runs against a scratch tree, the same extraction the forum.url step
      uses (tests/test_set_forum_url_3557.py). The dry-run plan lines are a second, cheaper cell.
- I2  the installer runs the same ``find`` at the end and exits non-zero, naming each path,
      when it lists anything; silent on a clean tree (which holds a published pack, a dm/
      at 2700 and a venv-style symlink, all of which must NOT trip it).
- I3  an existing ``$SERVICE_DIR/dm`` ends ``2700`` with no ACL (``setfacl -b`` then
      ``chmod 2700``: ``setfacl -b`` alone lands 2750).
- U1  all three rendered units carry ``UMask=0022``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))
from forum.tests.test_backup_alarm_units import _parse_unit, _rendered_copy  # noqa: E402

DEPLOY = _REPO / "src" / "forum" / "deploy"
INSTALLER = DEPLOY / "install-forum-service.sh"
UNITS = {
    "forum": DEPLOY / "engram-forum.service.template",
    "backup": DEPLOY / "engram-forum-backup.service.template",
    "alarm": DEPLOY / "engram-forum-backup-alarm.service.template",
}

_BLOCK_RE = re.compile(r"^# >>> service-modes\n(.*?)^# <<< service-modes$", re.S | re.M)
_RUN_RE = re.compile(r"^RUN\(\) \{\n.*?^\}$", re.S | re.M)


def _block() -> str:
    m = _BLOCK_RE.search(INSTALLER.read_text())
    assert m, "install-forum-service.sh has no '# >>> service-modes' block"
    return m.group(1)


def _run_fn() -> str:
    m = _RUN_RE.search(INSTALLER.read_text())
    assert m, "install-forum-service.sh has no RUN() wrapper"
    return m.group(0)


def _bash(svc: Path, body: str, *, mask: int, dry: int = 0) -> subprocess.CompletedProcess:
    script = (
        "set -euo pipefail\n"
        f"umask {mask:04o}\n"
        f'SERVICE_DIR="{svc}"\nAPP="$SERVICE_DIR/app"\nVENV="$SERVICE_DIR/.venv"\n'
        f'GROUP="$(id -gn)"\nDRY_RUN={dry}\n'
        + _run_fn() + "\n" + _block() + "\n" + body + "\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": str(svc.parent)})


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _legacy_tree(svc: Path, *, pack: bool, dm: bool, dm_mode: int = 0o2775) -> None:
    """A tree the way the OLD installer + a UMask=002 unit left it: g+w everywhere."""
    def put(rel: str, data: bytes = b"x", mode: int = 0o664) -> None:
        p = svc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.chmod(p, mode)

    svc.mkdir(parents=True, exist_ok=True)
    put("app/forum/server.py")
    put(".venv/lib/python3/site.py")
    (svc / ".venv" / "bin").mkdir(parents=True)
    (svc / ".venv" / "bin" / "python").symlink_to("/usr/bin/env")  # a symlink always reads 777
    put("forum.db")
    put("forum-audit.jsonl")
    put("backup/forum.sql")
    if pack:
        put("packs/alice__demo__v1/package.tar.gz")
        put("packs/alice__demo__v1/meta.json")
    if dm:
        put("dm/ariadne__sol.md", mode=0o660)
        os.chmod(svc / "dm", dm_mode)
    for d in [svc, *[p for p in svc.rglob("*") if p.is_dir() and not p.is_symlink()]]:
        if d != svc / "dm":
            os.chmod(d, 0o2775)


def _find_g_w(svc: Path) -> list[str]:
    r = subprocess.run(["find", str(svc), "-xdev", "-perm", "-g+w", "!", "-type", "l"],
                       capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln]


PHASE = "_set_service_modes; _normalize_service_modes"


# ---------------------------------------------------------------------------
# I1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dm", [False, True])
@pytest.mark.parametrize("pack", [False, True])
@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i1_no_group_writable_path_survives_the_mode_phase(tmp_path, mask, pack, dm):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=pack, dm=dm)
    assert _find_g_w(svc), "the fixture tree is not group-writable: the cell would be vacuous"
    r = _bash(svc, PHASE, mask=mask)
    assert r.returncode == 0, r.stderr
    assert _find_g_w(svc) == []
    if pack:
        assert os.access(svc / "packs" / "alice__demo__v1" / "package.tar.gz", os.R_OK)
    assert os.access(svc / "app" / "forum" / "server.py", os.R_OK)


@pytest.mark.parametrize("mask", [0o002, 0o022])
@pytest.mark.parametrize("existing", [0o2775, 0o775, None])
def test_i1_the_service_dir_itself_ends_2755_and_the_shell_umask_is_022(tmp_path, mask, existing):
    svc = tmp_path / "forum"
    if existing is not None:
        svc.mkdir()
        os.chmod(svc, existing)
    r = _bash(svc, '_set_service_modes\necho "UMASK=$(umask)"', mask=mask)
    assert r.returncode == 0, r.stderr
    assert "UMASK=0022" in r.stdout
    assert _mode(svc) == 0o2755, oct(_mode(svc))


@pytest.mark.parametrize("target_kind", ["file", "dir"])
@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i1_the_normalize_step_never_chmods_through_a_symlink(tmp_path, mask, target_kind):
    """A symlink inside the tree whose target is OUTSIDE it and owned by the test user: a
    chmod through the link would succeed (unlike the fixture's root-owned /usr/bin/env)."""
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=False)
    outside = tmp_path / "elsewhere"
    if target_kind == "file":
        outside.write_text("x")
        os.chmod(outside, 0o664)
        want = 0o664
    else:
        outside.mkdir()
        os.chmod(outside, 0o775)
        want = 0o775
    (svc / "app" / "link-out").symlink_to(outside)
    r = _bash(svc, PHASE, mask=mask)
    assert r.returncode == 0, r.stderr
    assert (svc / "app" / "link-out").is_symlink()
    assert _mode(outside) == want, f"chmod went through the symlink: {oct(_mode(outside))}"
    assert _find_g_w(svc) == []


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i1_the_dry_run_plan_prints_the_new_modes_and_not_the_old(tmp_path, mask):
    svc = tmp_path / "forum"
    r = subprocess.run(
        ["bash", "-c", f"umask {mask:04o}; bash {INSTALLER} --src {_REPO} --service-dir {svc} "
                       f"--group agents --dry-run"],
        capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert f"[dry-run] chmod 2755 {svc}" in out
    assert "[dry-run] umask 022" in out
    assert "chmod 2775" not in out and "umask 002" not in out
    assert not svc.exists(), "--dry-run created the service dir"


def test_i1_the_installer_no_longer_asks_for_group_write_anywhere():
    text = INSTALLER.read_text()
    assert "2775" not in text
    assert "umask 002" not in text
    assert "group-writable" not in text


# ---------------------------------------------------------------------------
# I2
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dm", [False, True])
@pytest.mark.parametrize("pack", [False, True])
@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i2_a_clean_tree_is_silent(tmp_path, mask, pack, dm):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=pack, dm=dm)
    r = _bash(svc, PHASE + "\n_assert_no_group_write", mask=mask)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "" and r.stderr.strip() == ""
    assert (svc / ".venv" / "bin" / "python").is_symlink()  # the 777 symlink was there and did not trip it


@pytest.mark.parametrize("planted", ["file", "dir", "nested-in-packs", "two"])
@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i2_a_planted_group_writable_path_fires_and_is_named(tmp_path, mask, planted):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=True, dm=True)
    targets = {
        "file": ["planted.txt"],
        "dir": ["plantdir"],
        "nested-in-packs": ["packs/alice__demo__v1/extra.bin"],
        "two": ["one.txt", "backup/two.txt"],
    }[planted]
    plant = "\n".join(
        (f'mkdir "$SERVICE_DIR/{t}"; chmod 775 "$SERVICE_DIR/{t}"' if planted == "dir"
         else f': > "$SERVICE_DIR/{t}"; chmod 664 "$SERVICE_DIR/{t}"')
        for t in targets
    )
    r = _bash(svc, PHASE + "\n" + plant + "\n_assert_no_group_write", mask=mask)
    assert r.returncode == 2, "the assertion stayed silent over a planted group-writable path"
    for t in targets:
        assert str(svc / t) in r.stderr, f"{t} not named in: {r.stderr!r}"


def test_i2_the_find_excludes_symlinks_so_a_clean_venv_does_not_fire(tmp_path):
    """Mutant (b) shape: drop ``! -type l`` and the clean tree's 777 venv symlink fires."""
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=False)
    r = _bash(svc, PHASE + "\n_assert_no_group_write", mask=0o002)
    assert r.returncode == 0, r.stderr
    assert "-type l" in _block()


def test_i2_the_installer_calls_the_assertion_before_done_and_does_not_swallow_it():
    text = INSTALLER.read_text()
    calls = [m for m in re.finditer(r"^\s*_assert_no_group_write\b(.*)$", text, re.M)]
    assert calls, "the installer never calls _assert_no_group_write"
    last = calls[-1]
    assert "|| true" not in last.group(1)
    assert last.start() < text.index('echo "==> Done."')
    assert last.start() > text.index("# <<< forum-url-step")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
@pytest.mark.parametrize("locked", [0o000, 0o100])
@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i2_an_uninspectable_subtree_fails_the_assertion(tmp_path, mask, locked):
    """An admin hand-off leaves a dir find cannot list: that is not a clean tree."""
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=False)
    sub = svc / "handed-off"
    sub.mkdir()
    (sub / "inner.txt").write_text("x")
    os.chmod(sub, locked)
    try:
        r = _bash(svc, PHASE + "\n_assert_no_group_write", mask=mask)
    finally:
        os.chmod(sub, 0o755)
    assert r.returncode == 2, "the assertion passed over a subtree it could not inspect"
    assert "could not be inspected" in r.stderr, r.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_i2_a_readable_subtree_control_passes(tmp_path):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=False)
    sub = svc / "handed-off"
    sub.mkdir()
    (sub / "inner.txt").write_text("x")
    os.chmod(sub / "inner.txt", 0o644)
    os.chmod(sub, 0o755)
    r = _bash(svc, PHASE + "\n_assert_no_group_write", mask=0o022)
    assert r.returncode == 0, r.stderr


def _symlinked_svc(tmp_path: Path, *, dirty: bool) -> tuple[Path, Path]:
    """svc is a SYMLINK to a real directory (the finding: find does not descend into it)."""
    real = tmp_path / "real-forum"
    real.mkdir()
    (real / "f.txt").write_text("x")
    (real / "d").mkdir()
    os.chmod(real / "f.txt", 0o664 if dirty else 0o644)
    os.chmod(real / "d", 0o775 if dirty else 0o755)
    os.chmod(real, 0o755)
    svc = tmp_path / "forum"
    svc.symlink_to(real)
    return svc, real


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i2_c1a_a_symlinked_service_dir_is_still_normalized(tmp_path, mask):
    svc, real = _symlinked_svc(tmp_path, dirty=True)
    r = _bash(svc, "_normalize_service_modes", mask=mask)
    assert r.returncode == 0, r.stderr
    assert _mode(real / "f.txt") & 0o020 == 0, oct(_mode(real / "f.txt"))
    assert _mode(real / "d") & 0o020 == 0, oct(_mode(real / "d"))


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i2_c1b_a_symlinked_service_dir_does_not_hide_a_group_writable_path(tmp_path, mask):
    svc, real = _symlinked_svc(tmp_path, dirty=True)
    r = _bash(svc, "_assert_no_group_write", mask=mask)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "f.txt" in r.stderr, r.stderr


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i2_c1c_a_symlinked_clean_service_dir_is_silent(tmp_path, mask):
    svc, _real = _symlinked_svc(tmp_path, dirty=False)
    r = _bash(svc, "_assert_no_group_write", mask=mask)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "" and r.stderr.strip() == ""


def test_i2_dry_run_prints_a_plan_line_and_does_not_run_the_find(tmp_path):
    svc = tmp_path / "forum"
    svc.mkdir()
    (svc / "x").write_text("x")
    os.chmod(svc / "x", 0o664)
    r = _bash(svc, "_normalize_service_modes\n_assert_no_group_write", mask=0o002, dry=1)
    assert r.returncode == 0, r.stderr
    assert "[dry-run]" in r.stdout
    assert _mode(svc / "x") == 0o664, "--dry-run changed a mode"


# ---------------------------------------------------------------------------
# I3
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i3_the_installer_body_normalizes_before_it_asserts(tmp_path, mask):
    """Behavioural (dry-run) route: the WHOLE installer prints the normalize plan line and
    then the assertion plan line. Deleting the installer's own normalize call drops the first."""
    svc = tmp_path / "forum"
    r = subprocess.run(
        ["bash", "-c", f"umask {mask:04o}; bash {INSTALLER} --src {_REPO} --service-dir {svc} "
                       f"--group agents --dry-run"],
        capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    norm = f"[dry-run] find -H {svc} -xdev ! -type l -exec chmod g-w {{}} +"
    asrt = f"[dry-run] assert: find -H {svc} -xdev -perm -g+w ! -type l lists nothing"
    assert out.count(norm) == 1, out
    assert out.count(asrt) == 1, out
    assert out.index(norm) < out.index(asrt)


def _getfacl(path: Path) -> str:
    if shutil.which("getfacl") is None:  # the ACL half is exercised only where getfacl exists (macOS has none)
        return ""
    return subprocess.run(["getfacl", "-n", "--skip-base", "--absolute-names", str(path)],
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("dm_mode", [0o2775, 0o755, 0o2750, 0o2700])
@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i3_an_existing_dm_ends_2700(tmp_path, mask, dm_mode):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=True, dm_mode=dm_mode)
    r = _bash(svc, PHASE, mask=mask)
    assert r.returncode == 0, r.stderr
    assert _mode(svc / "dm") == 0o2700, oct(_mode(svc / "dm"))
    assert _mode(svc / "dm") & 0o077 == 0
    assert _getfacl(svc / "dm") == ""


@pytest.mark.skipif(shutil.which("setfacl") is None or shutil.which("getfacl") is None,
                    reason="needs setfacl/getfacl")
@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i3_a_named_and_default_acl_is_removed(tmp_path, mask):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=True)
    probe = subprocess.run(["setfacl", "-m", "u:65534:rwx", str(svc / "dm")], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"this filesystem refuses ACLs: {probe.stderr.strip()}")
    subprocess.run(["setfacl", "-d", "-m", "u:65534:rwx", str(svc / "dm")], check=True)
    assert "65534" in _getfacl(svc / "dm"), "the fixture ACL did not take: the cell would be vacuous"
    r = _bash(svc, PHASE, mask=mask)
    assert r.returncode == 0, r.stderr
    assert _getfacl(svc / "dm") == ""
    assert _mode(svc / "dm") == 0o2700


@pytest.mark.parametrize("mask", [0o002, 0o022])
def test_i3_no_dm_means_none_is_created(tmp_path, mask):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=False)
    r = _bash(svc, PHASE, mask=mask)
    assert r.returncode == 0, r.stderr
    assert not (svc / "dm").exists()


def test_i3_a_dm_that_is_a_symlink_is_not_followed(tmp_path):
    svc = tmp_path / "forum"
    _legacy_tree(svc, pack=False, dm=False)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.chmod(outside, 0o755)
    (svc / "dm").symlink_to(outside)
    r = _bash(svc, PHASE, mask=0o002)
    assert r.returncode == 0, r.stderr
    assert _mode(outside) == 0o755, "the installer chmod'd the target of a dm symlink"


# ---------------------------------------------------------------------------
# U1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unit", list(UNITS))
def test_u1_every_rendered_unit_carries_exactly_umask_0022(tmp_path, unit):
    rendered = _rendered_copy(UNITS[unit], tmp_path)
    service = _parse_unit(rendered).get("Service", {})
    assert service.get("UMask") == ["0022"], service.get("UMask")


@pytest.mark.parametrize("unit", list(UNITS))
def test_u1_no_unit_template_still_claims_group_write(unit):
    text = UNITS[unit].read_text()
    assert "UMask=002\n" not in text
    assert "group-writable" not in text
