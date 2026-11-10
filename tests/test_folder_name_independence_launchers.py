"""Folder-name independence, launchers (S1, S2): the checkout may be `engram` or `engram-alpha`.

Contract (REVISION 2: `engram` is the default and is tried FIRST; `engram-alpha` stays accepted):
  S1 agent-session-launch.sh launch cwd
    C1.1 only $HOME/engram exists            -> cd $HOME/engram, arm names `$HOME/engram`
    C1.2 only $HOME/engram-alpha exists      -> unchanged result and text
    C1.3 both exist, both trusted            -> engram wins
    C1.4 engram untrusted, alpha trusted     -> alpha (trust beats order); and the mirror case -> engram
    C1.5 neither exists                      -> umwelt, then $HOME, as before
    C1.6 ~/.engram/boot-cwd still wins over every candidate
  S2 macos/clio-wake-launch.sh launch cwd
    C2.1 only $HOME/GitHub/engram exists     -> that
    C2.2 only $HOME/GitHub/engram-alpha      -> unchanged
    C2.3 both exist                          -> engram
    C2.4 neither                             -> $HOME; boot-cwd override unchanged
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_launch_cwd_trust_4170 import _boot_lines, _errors, _launch  # noqa: E402
from test_clio_wake_launchd import TestLaunchQuietSkip  # noqa: E402


def _trust(*names):
    return {"projects": {f"@HOME@/{n}": {"hasTrustDialogAccepted": True} for n in names}}


# ---- S1 ---------------------------------------------------------------------

def test_C1_1_only_engram_untrusted(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram",))
    assert proc.returncode == 0, proc.stderr
    assert argv[-1] == str(home / "engram")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram (from $HOME/engram, none trusted)"]
    assert len(_errors(proc)) == 1 and str(home / "engram") in _errors(proc)[0]


def test_C1_1_only_engram_trusted(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram",), claude_json=_trust("engram"))
    assert argv[-1] == str(home / "engram")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram (from $HOME/engram, first trusted)"]
    assert _errors(proc) == []


def test_C1_1_engram_beats_umwelt(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram", "umwelt"))
    assert argv[-1] == str(home / "engram")


def test_C1_2_only_alpha_unchanged(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha",))
    assert argv[-1] == str(home / "engram-alpha")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram-alpha (from $HOME/engram-alpha, none trusted)"]


def test_C1_3_both_trusted_engram_wins(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "engram"),
                                   claude_json=_trust("engram-alpha", "engram"))
    assert argv[-1] == str(home / "engram")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram (from $HOME/engram, first trusted)"]


def test_C1_3b_both_exist_none_trusted_engram_first(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "engram"))
    assert argv[-1] == str(home / "engram")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram (from $HOME/engram, none trusted)"]


def test_C1_4_engram_untrusted_alpha_trusted_alpha_wins(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "engram"),
                                   claude_json=_trust("engram-alpha"))
    assert argv[-1] == str(home / "engram-alpha")
    assert _boot_lines(proc) == [f"boot cwd: {home}/engram-alpha (from $HOME/engram-alpha, first trusted)"]
    assert _errors(proc) == []


def test_C1_4b_alpha_untrusted_engram_trusted_engram_wins(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("engram-alpha", "engram"),
                                   claude_json=_trust("engram"))
    assert argv[-1] == str(home / "engram")
    assert _errors(proc) == []


def test_C1_5_neither_umwelt(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("umwelt",))
    assert argv[-1] == str(home / "umwelt")
    assert _boot_lines(proc) == [f"boot cwd: {home}/umwelt (from $HOME/umwelt, none trusted)"]


def test_C1_5_neither_home(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path)
    assert argv[-1] == str(home)
    assert _boot_lines(proc) == [f"boot cwd: {home} (from $HOME, none trusted)"]


def test_C1_6_boot_cwd_beats_both(tmp_path):
    proc, home, argv, *_ = _launch(tmp_path, dirs=("custom", "engram-alpha", "engram"),
                                   boot_cwd="@HOME@/custom", claude_json=_trust("engram"))
    assert argv[-1] == str(home / "custom")
    assert _boot_lines(proc) == [f"boot cwd: {home}/custom (from boot-cwd)"]


# ---- S2 ---------------------------------------------------------------------

def _clio_cd_target(tmp_path, dirs, boot_cwd=None):
    home = tmp_path / "home"
    for d in dirs:
        (home / d).mkdir(parents=True)
    if boot_cwd is not None:
        (home / ".engram").mkdir(parents=True, exist_ok=True)
        (home / ".engram" / "boot-cwd").write_text(boot_cwd.replace("@HOME@", str(home)) + "\n")
    result, marker, _log, _ = TestLaunchQuietSkip()._run_launch(tmp_path, claude_process_live=False)
    assert result.returncode == 0, result.stderr
    args = marker.read_bytes().split(b"\0")[:-1]
    return args[-1].decode(), home


def test_C2_1_only_new_name(tmp_path):
    got, home = _clio_cd_target(tmp_path, ["GitHub/engram"])
    assert got == str(home / "GitHub" / "engram")


def test_C2_2_only_old_name(tmp_path):
    got, home = _clio_cd_target(tmp_path, ["GitHub/engram-alpha"])
    assert got == str(home / "GitHub" / "engram-alpha")


def test_C2_3_both_engram_wins(tmp_path):
    got, home = _clio_cd_target(tmp_path, ["GitHub/engram-alpha", "GitHub/engram"])
    assert got == str(home / "GitHub" / "engram")


def test_C2_4_neither_is_home(tmp_path):
    got, home = _clio_cd_target(tmp_path, [])
    assert got == str(home)


def test_C2_4_boot_cwd_override_unchanged(tmp_path):
    got, home = _clio_cd_target(tmp_path, ["custom", "GitHub/engram"], boot_cwd="@HOME@/custom")
    assert got == str(home / "custom")
