"""Contract-first tests: the heartbeat cadence is a config field, team default 900.

Lei, 2026-10-23: the cadence is 15 minutes for the team, not a 30-minute value
hardcoded in the boot charge. The field is ``loop.heartbeat_cadence_seconds`` in
``$ENGRAM_HOME/config.json``; ``loop_prompt.configured_cadence_seconds()`` is the
ONE resolver; ``loop_prompt.py --cadence`` prints it; a valid marker
``cadence_seconds`` still wins on ``--rearm-delay``.

Contract (written before the implementation):
  * config absent / no ``loop`` / no key            -> 900, silent
  * key a positive int (bool is NOT an int)         -> that value, silent
  * key present but invalid (0, negative, bool, float, "900", null) or ``loop``
    not an object                                   -> 900 + exactly ONE stderr line
                                                       naming the field and the bad value
  * config.json unreadable / not valid JSON         -> 900 + ONE stderr line naming the
                                                       file; never raises
  * --rearm-delay, marker without cadence           -> the configured value
  * --rearm-delay, valid marker cadence             -> the marker's value (marker wins)
  * --cadence                                       -> the configured value, bare int
  * prose: both Claude boot charges point at ``loop_prompt.py --cadence``, no longer
    say ``pace at (1800``, and carry the identical cadence sentences.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LP_PATH = ROOT / "src" / "engram" / "tools" / "loop_prompt.py"
_spec = importlib.util.spec_from_file_location("loop_prompt_cadence_cfg_test", LP_PATH)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)

CHARGES = [
    ROOT / "deploy" / "autostart" / "boot-charge-default-claude.md",
    ROOT / "deploy" / "autostart" / "boot-charge-quiet-claude.md",
]


def _home(tmp_path, monkeypatch, config=None, raw=None):
    home = tmp_path / "home"
    home.mkdir()
    if config is not None:
        (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if raw is not None:
        (home / "config.json").write_bytes(raw)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return home


def _cli(home, *args):
    env = dict(os.environ, ENGRAM_HOME=str(home))
    return subprocess.run(
        [sys.executable, str(LP_PATH), *args], capture_output=True, text=True, env=env
    )


# --- resolver: silent cells -------------------------------------------------

@pytest.mark.parametrize(
    "config",
    [None, {}, {"loop": {}}, {"loop": {"other": 1}}],
    ids=["no_file", "no_loop", "loop_without_key", "loop_other_key"],
)
def test_absent_yields_default_silently(tmp_path, monkeypatch, capsys, config):
    _home(tmp_path, monkeypatch, config=config)
    assert lp.configured_cadence_seconds() == 900
    assert capsys.readouterr().err == ""


def test_team_default_constant_is_900():
    assert lp.DEFAULT_CADENCE_SECONDS == 900


@pytest.mark.parametrize("value", [60, 300, 1200, 3300])
def test_valid_positive_int_is_returned_silently(tmp_path, monkeypatch, capsys, value):
    _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": value}})
    assert lp.configured_cadence_seconds() == value
    assert capsys.readouterr().err == ""


# --- resolver: loud cells ---------------------------------------------------

@pytest.mark.parametrize(
    "bad,shown",
    [(0, "0"), (-5, "-5"), (True, "True"), (False, "False"), (900.0, "900.0"),
     ("900", "'900'"), (None, "None"), (15, "15"), (59, "59")],
    ids=["zero", "negative", "true", "false", "float", "string", "null", "below_min_15",
         "below_min_59"],
)
def test_invalid_value_is_default_plus_one_loud_line(tmp_path, monkeypatch, capsys, bad, shown):
    _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": bad}})
    assert lp.configured_cadence_seconds() == 900
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, err
    assert "loop.heartbeat_cadence_seconds" in lines[0]
    assert shown in lines[0]


@pytest.mark.parametrize("loop", ["fast", 5, [1], None], ids=["str", "int", "list", "null"])
def test_loop_not_an_object_is_default_plus_one_loud_line(tmp_path, monkeypatch, capsys, loop):
    _home(tmp_path, monkeypatch, config={"loop": loop})
    assert lp.configured_cadence_seconds() == 900
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1 and "loop.heartbeat_cadence_seconds" in lines[0]


@pytest.mark.parametrize("raw", [b"{not json", b"", b"\xff\xfe\x00bad"], ids=["garbage", "empty", "binary"])
def test_unparseable_config_is_default_plus_one_line_naming_file(tmp_path, monkeypatch, capsys, raw):
    _home(tmp_path, monkeypatch, raw=raw)
    assert lp.configured_cadence_seconds() == 900
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1 and "config.json" in lines[0]


def test_unreadable_config_never_raises(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch)
    (home / "config.json").mkdir()  # a directory: open() raises IsADirectoryError
    assert lp.configured_cadence_seconds() == 900
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1 and "config.json" in lines[0]


def test_non_object_config_root_is_loud(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch, raw=b"[1, 2]")
    assert lp.configured_cadence_seconds() == 900
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1 and "config.json" in lines[0]


def test_below_minimum_names_the_field_and_the_minimum(tmp_path, monkeypatch, capsys):
    """A value under _MIN_DELAY_SECONDS (60) is invalid: the line says why, not just 'not positive'."""
    _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": 59}})
    assert lp.configured_cadence_seconds() == 900
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "loop.heartbeat_cadence_seconds" in lines[0]
    assert f"minimum {lp._MIN_DELAY_SECONDS}" in lines[0]


_DEEP = b"[" * 100000 + b"]" * 100000


def test_deeply_nested_config_is_default_plus_one_line_no_traceback(tmp_path, monkeypatch, capsys):
    """json.load raises RecursionError (not ValueError) on absurd nesting; it must not escape."""
    _home(tmp_path, monkeypatch, raw=_DEEP)
    assert lp.configured_cadence_seconds() == 900
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1 and "config.json" in lines[0]


def test_cli_cadence_deeply_nested_config_no_traceback(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, raw=_DEEP)
    r = _cli(home, "--cadence")
    assert (r.returncode, r.stdout) == (0, "900\n"), r.stderr
    assert "Traceback" not in r.stderr
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "config.json" in lines[0]


# --- CLI --------------------------------------------------------------------

def test_cli_cadence_prints_configured_value(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": 1200}})
    r = _cli(home, "--cadence")
    assert (r.returncode, r.stdout, r.stderr) == (0, "1200\n", "")


def test_cli_cadence_absent_prints_900(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    r = _cli(home, "--cadence")
    assert (r.returncode, r.stdout, r.stderr) == (0, "900\n", "")


def test_cli_rearm_without_marker_cadence_uses_configured(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": 1200}})
    marker = home / "loop-mode.json"
    marker.write_text(json.dumps({"kind": "loop"}), encoding="utf-8")
    r = _cli(home, "--rearm-delay", "--wake-kind", "heartbeat", "--marker", str(marker))
    assert (r.returncode, r.stdout) == (0, "1200\n"), r.stderr


def test_cli_rearm_marker_cadence_wins_over_config(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": 1200}})
    marker = home / "loop-mode.json"
    marker.write_text(json.dumps({"cadence_seconds": 600}), encoding="utf-8")
    r = _cli(home, "--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert (r.returncode, r.stdout) == (0, "600\n"), r.stderr


def test_cli_rearm_invalid_marker_cadence_falls_back_to_configured(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": 1200}})
    marker = home / "loop-mode.json"
    marker.write_text(json.dumps({"cadence_seconds": "oops"}), encoding="utf-8")
    r = _cli(home, "--rearm-delay", "--marker", str(marker))
    assert r.returncode == 0 and r.stdout == "1200\n"
    assert "invalid_cadence" in r.stderr and "1200s" in r.stderr


def test_cli_rearm_valid_marker_never_reads_the_config(tmp_path, monkeypatch):
    """Lazy read: a valid marker cadence means config.json is not consulted at all, so an
    invalid config produces NO invalid_cadence_config line."""
    home = _home(tmp_path, monkeypatch, config={"loop": {"heartbeat_cadence_seconds": "abc"}})
    marker = home / "loop-mode.json"
    marker.write_text(json.dumps({"cadence_seconds": 600}), encoding="utf-8")
    r = _cli(home, "--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert (r.returncode, r.stdout) == (0, "600\n"), r.stderr
    assert "invalid_cadence_config" not in r.stderr
    assert r.stderr == ""


def test_cli_cadence_and_rearm_delay_are_mutually_exclusive(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    r = _cli(home, "--cadence", "--rearm-delay")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "not allowed with" in r.stderr


# --- prose guard ------------------------------------------------------------

@pytest.mark.parametrize("path", CHARGES, ids=lambda p: p.name)
def test_boot_charge_reads_cadence_from_config(path):
    text = path.read_text(encoding="utf-8")
    assert "loop_prompt.py\" --cadence" in text or "loop_prompt.py --cadence" in text
    assert "pace at (1800" not in text
    assert "still hold the 1800s floor" not in text
    assert "loop.heartbeat_cadence_seconds" in text


def _all(text, needle):
    i = text.find(needle)
    while i != -1:
        yield i
        i = text.find(needle, i + 1)


def test_boot_charge_cadence_sentences_identical_across_charges():
    markers = (
        "the interval you intend to pace at (",
        "the configured cadence is the team's standing default (",
        "still hold the configured floor",
    )

    def sentences(p):
        t = p.read_text(encoding="utf-8")
        return [[t[i:i + 400] for i in _all(t, m)] for m in markers]

    a, b = (sentences(p) for p in CHARGES)
    assert all(a), "cadence sentence markers missing"
    assert a == b
