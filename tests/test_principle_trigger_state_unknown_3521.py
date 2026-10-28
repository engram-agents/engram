"""Contract-first tests for #3521 row 40: a corrupt principle-trigger-state.json
is UNKNOWN and is moved aside, never silently overwritten.

`engram-surface-hook.py::_check_principle_triggers_inner` reads
`principle-trigger-state.json` (the per-principle cooldown / strength /
enactments / fires history). Before this row, bad JSON or a non-dict was read as
`{}` silently, and the write-back (under `_with_state_lock`) then rewrote the
file holding only the principles rendered this prompt: every other principle's
history AND the corrupt bytes were gone, with no stderr and nothing injected.

The contract (maintainer rulings, binding):

  1. One read rule (`_hooklib.read_state_json_classified`): ABSENT -> `{}`
     silently; a JSON object -> unchanged; anything else on a regular file
     (bad JSON, a non-dict, an unreadable file) -> UNKNOWN; a non-regular
     target (FIFO, directory) -> UNKNOWN but never moved and never written.
  2. UNKNOWN has its own probe key `principle-trigger-state` (not row 7's
     `principle-triggers`). The firing decision is UNCHANGED: state is treated
     as empty for the cooldown decision (an extra nudge, never a missing one).
  3. Under the SAME lock the write-back holds, a fresh in-lock read that is
     UNKNOWN for a regular file is moved (`os.replace`) to
     `principle-trigger-state.json.corrupt` -- ONE fixed name, the latest
     corruption only -- and THEN fresh state is written. Never a plain overwrite.
  4. No fire this prompt -> reported UNKNOWN, but not moved and not written.
  5. The healthy path is byte-identical to the merge-base copy of the hook.
"""

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# #4091: the hook loads below run its module-level sys.path.insert; the shared window
# (#4029) puts sys.path and the src/engram modules back after each test.
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
_SURFACE_PATH = HOOK_DIR / "engram-surface-hook.py"
_REL = "src/engram/hooks/claude/engram-surface-hook.py"

STATE_NAME = "principle-trigger-state.json"
SIDECAR_NAME = "principle-trigger-state.json.corrupt"
STAMP_NAME = "probe-unknown-notice.json"
TRIG = "tr_r40"
TRIG_BOGUS = "tr_r40bogus"
PID = "ls_r40a"
OTHER = "ls_r40b"

REGISTRY = {
    TRIG: [{
        "principle_id": PID,
        "kind": "lesson",
        "claim": "r40 claim text",
        "nudge": "r40 nudge text",
    }],
    # A kind the render loop does not know: filtered out before the cap, so a
    # prompt matching only this id renders nothing (no write-back).
    TRIG_BOGUS: [{
        "principle_id": "ls_r40c",
        "kind": "bogus",
        "claim": "x",
        "nudge": "y",
    }],
}

# A RICH state: two principles with real history, so "every other principle's
# entry is lost" is observable.
RICH_STATE = {
    PID: {"last_fired_prompt": 1, "strength": 0.7, "enactments": 3, "fires": 4},
    OTHER: {"last_fired_prompt": 2, "strength": 0.9, "enactments": 1, "fires": 6},
}

CORRUPT_A = b'{"ls_r40a": {"last_fired_prompt": 1, "strength": 0.7, "enac'
CORRUPT_B = b'{"ls_r40b": {"fires": 6, "last_fired_prompt": 2, "strength": 0.9, "x'
LIST_BYTES = b'[1, 2, 3]'

_COUNTER = 0


def _load(path: Path, name: str):
    global _COUNTER
    _COUNTER += 1
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    spec = importlib.util.spec_from_file_location(f"{name}_{_COUNTER}", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _surface(monkeypatch, home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return _load(_SURFACE_PATH, "surface_hook_3521r40")


def _hooklib():
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    import _hooklib
    return _hooklib


def _home(tmp_path: Path, name="home") -> Path:
    h = tmp_path / name
    h.mkdir()
    (h / "principle_triggers.json").write_text(json.dumps(REGISTRY))
    return h


def _plant(home: Path, data: bytes) -> Path:
    p = home / STATE_NAME
    p.write_bytes(data)
    return p


def _call(mod, ids=(TRIG,), count=5, state_unknown="list"):
    if state_unknown == "list":
        su = []
        return mod.check_principle_triggers_full(list(ids), count, state_unknown=su), su
    return mod.check_principle_triggers_full(list(ids), count), None


def _files(home: Path):
    return sorted(p.name for p in home.iterdir())


def _hook_files(home: Path):
    # The telemetry emitter binds its logs/ + telemetry/ dirs to the first
    # ENGRAM_HOME seen in the process; they are not part of this contract.
    return [n for n in _files(home) if n not in ("logs", "telemetry")]


# ---------------------------------------------------------------------------
# S1 helper classification table
# ---------------------------------------------------------------------------

class TestHelperClassification:
    def test_absent(self, tmp_path):
        h = _hooklib()
        assert h.read_state_json_classified(str(tmp_path / "nope.json")) == ({}, None, False)

    def test_dict(self, tmp_path):
        h = _hooklib()
        p = tmp_path / "s.json"
        p.write_text(json.dumps(RICH_STATE))
        assert h.read_state_json_classified(str(p)) == (RICH_STATE, None, False)

    def test_empty_dict_is_valid(self, tmp_path):
        h = _hooklib()
        p = tmp_path / "s.json"
        p.write_text("{}")
        assert h.read_state_json_classified(str(p)) == ({}, None, False)

    def test_bad_json(self, tmp_path):
        h = _hooklib()
        p = tmp_path / "s.json"
        p.write_bytes(CORRUPT_A)
        state, reason, movable = h.read_state_json_classified(str(p))
        assert state == {} and movable is True
        assert reason.startswith("JSONDecodeError: "), reason

    def test_empty_file_is_bad_json(self, tmp_path):
        h = _hooklib()
        p = tmp_path / "s.json"
        p.write_bytes(b"")
        state, reason, movable = h.read_state_json_classified(str(p))
        assert state == {} and movable is True
        assert reason.startswith("JSONDecodeError: "), reason

    @pytest.mark.parametrize("raw,tname", [
        (b"[1, 2, 3]", "list"), (b"null", "NoneType"), (b"0", "int"), (b'"s"', "str"),
    ])
    def test_non_dict(self, raw, tname, tmp_path):
        h = _hooklib()
        p = tmp_path / "s.json"
        p.write_bytes(raw)
        state, reason, movable = h.read_state_json_classified(str(p))
        assert state == {} and movable is True
        assert reason.startswith("NotADict: "), reason
        assert tname in reason, reason

    def test_mode_000_regular_file(self, tmp_path):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root bypasses mode 000")
        h = _hooklib()
        p = tmp_path / "s.json"
        p.write_text(json.dumps(RICH_STATE))
        p.chmod(0)
        try:
            state, reason, movable = h.read_state_json_classified(str(p))
        finally:
            p.chmod(0o600)
        # An unreadable file has not been shown unparseable: UNKNOWN, never movable.
        assert state == {} and movable is False
        assert reason.startswith("PermissionError: "), reason

    def test_directory_is_unknown_not_movable(self, tmp_path):
        h = _hooklib()
        d = tmp_path / "s.json"
        d.mkdir()
        state, reason, movable = h.read_state_json_classified(str(d))
        assert state == {} and movable is False
        assert reason and ": " in reason, reason

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo")
    def test_fifo_is_unknown_not_movable_and_does_not_hang(self, tmp_path):
        h = _hooklib()
        f = tmp_path / "s.json"
        os.mkfifo(str(f))
        state, reason, movable = h.read_state_json_classified(str(f))
        assert state == {} and movable is False
        assert reason and ": " in reason, reason

    def test_reason_feeds_probe_helpers(self, tmp_path):
        h = _hooklib()
        p = tmp_path / "s.json"
        p.write_bytes(CORRUPT_A)
        _, reason, _ = h.read_state_json_classified(str(p))
        assert h.split_probe_reason(reason)[0] == "JSONDecodeError"


# ---------------------------------------------------------------------------
# S2 / S3 / S4 / S6 move aside
# ---------------------------------------------------------------------------

class TestMoveAside:
    def test_s2_corrupt_bytes_kept_and_fresh_state_written(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        (text, rendered), su = _call(mod)
        assert "r40 claim text" in text and len(rendered) == 1
        side = home / SIDECAR_NAME
        assert side.exists()
        assert side.read_bytes() == CORRUPT_A
        live = json.loads((home / STATE_NAME).read_text())
        assert isinstance(live, dict)
        assert live[PID]["last_fired_prompt"] == 5
        assert live[PID]["fires"] == 1
        assert OTHER not in live  # lives only in the sidecar
        assert len(su) == 1, su
        assert su[0]["reason"].startswith("JSONDecodeError: ")
        assert su[0]["moved_to"] == str(side)

    def test_s3_only_latest_corrupt_copy_is_kept(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        _call(mod)
        assert (home / SIDECAR_NAME).read_bytes() == CORRUPT_A
        _plant(home, CORRUPT_B)
        (_t, _r), su = _call(mod, count=6)
        assert (home / SIDECAR_NAME).read_bytes() == CORRUPT_B
        assert su[0]["moved_to"] == str(home / SIDECAR_NAME)
        assert _files(home) == sorted([
            "principle_triggers.json", STATE_NAME, SIDECAR_NAME, STATE_NAME + ".lock",
        ]), _files(home)

    def test_s4_firing_decision_unchanged(self, tmp_path, monkeypatch):
        home_c = _home(tmp_path, "corrupt")
        _plant(home_c, CORRUPT_A)
        home_a = _home(tmp_path, "absent")
        mod_c = _surface(monkeypatch, home_c)
        (text_c, rendered_c), _ = _call(mod_c)
        mod_a = _surface(monkeypatch, home_a)
        (text_a, rendered_a), _ = _call(mod_a)
        assert text_c and text_c == text_a
        assert rendered_c == rendered_a

    def test_s6_list_state_is_moved_aside(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, LIST_BYTES)
        mod = _surface(monkeypatch, home)
        (text, _rendered), su = _call(mod)
        assert "r40 claim text" in text
        assert (home / SIDECAR_NAME).read_bytes() == LIST_BYTES
        assert isinstance(json.loads((home / STATE_NAME).read_text()), dict)
        assert su[0]["reason"].startswith("NotADict: "), su
        assert su[0]["moved_to"] == str(home / SIDECAR_NAME)

    def test_mode_000_state_is_unknown_not_moved_not_written(self, tmp_path, monkeypatch):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root bypasses mode 000")
        home = _home(tmp_path)
        raw = json.dumps(RICH_STATE).encode()
        p = _plant(home, raw)
        p.chmod(0)
        mod = _surface(monkeypatch, home)
        try:
            (text, _r), su = _call(mod)
            assert "r40 claim text" in text  # firing decision unchanged
            assert not (home / SIDECAR_NAME).exists()
            assert su[0]["reason"].startswith("PermissionError: "), su
            assert su[0]["moved_to"] is None
        finally:
            p.chmod(0o600)
        assert p.read_bytes() == raw  # not moved, not written

    def test_hooklib_fallback_path_moves_aside_and_writes_dict(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        mod._HOOKLIB_OK = False  # the tmp + os.replace fallback writer
        (text, _r), su = _call(mod)
        assert "r40 claim text" in text
        assert (home / SIDECAR_NAME).read_bytes() == CORRUPT_A
        live = json.loads((home / STATE_NAME).read_text())
        assert isinstance(live, dict) and live[PID]["last_fired_prompt"] == 5
        assert su[0]["moved_to"] == str(home / SIDECAR_NAME)

    def test_snapshot_corrupt_but_repaired_before_lock_is_not_moved(self, tmp_path, monkeypatch):
        # The move decision uses the FRESH in-lock read: a file repaired between
        # the snapshot read and the lock is merged into, never moved aside.
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        real_lock = mod._with_state_lock

        def repair_then_lock(lock_path, fn):
            (home / STATE_NAME).write_text(json.dumps(RICH_STATE))
            return real_lock(lock_path, fn)

        monkeypatch.setattr(mod, "_with_state_lock", repair_then_lock)
        (text, _r), su = _call(mod)
        assert "r40 claim text" in text
        assert not (home / SIDECAR_NAME).exists()
        live = json.loads((home / STATE_NAME).read_text())
        assert OTHER in live and live[OTHER] == RICH_STATE[OTHER]
        assert live[PID]["last_fired_prompt"] == 5 and live[PID]["fires"] == 5
        assert len(su) == 1 and su[0]["moved_to"] is None  # snapshot was UNKNOWN

    def test_state_unknown_none_still_moves_aside(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        (text, _r), su = _call(mod, state_unknown=None)
        assert su is None and "r40 claim text" in text
        assert (home / SIDECAR_NAME).read_bytes() == CORRUPT_A

    def test_failed_move_never_falls_back_to_overwrite(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        real_replace = os.replace

        def failing_replace(src, dst, *a, **kw):
            if str(dst).endswith(".corrupt"):
                raise PermissionError(13, "synthetic")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(mod.os, "replace", failing_replace)
        (text, _r), su = _call(mod)
        assert "r40 claim text" in text
        assert (home / STATE_NAME).read_bytes() == CORRUPT_A
        assert not (home / SIDECAR_NAME).exists()
        assert su[0]["moved_to"] is None


# ---------------------------------------------------------------------------
# S5 no-fire prompt
# ---------------------------------------------------------------------------

class TestNoFire:
    def test_s5_nothing_rendered_reports_but_does_not_move(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        (res, su) = _call(mod, ids=(TRIG_BOGUS,))
        assert res == ("", [])
        assert (home / STATE_NAME).read_bytes() == CORRUPT_A
        assert not (home / SIDECAR_NAME).exists()
        assert len(su) == 1 and su[0]["reason"].startswith("JSONDecodeError: ")
        assert su[0]["moved_to"] is None

    def test_empty_matched_ids_returns_before_state_read(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        su = []
        assert mod.check_principle_triggers_full([], 5, state_unknown=su) == ("", [])
        assert su == []
        assert (home / STATE_NAME).read_bytes() == CORRUPT_A

    def test_absent_and_valid_state_report_nothing(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        mod = _surface(monkeypatch, home)
        _, su = _call(mod)
        assert su == []
        assert not (home / SIDECAR_NAME).exists()
        home2 = _home(tmp_path, "valid")
        _plant(home2, json.dumps(RICH_STATE).encode())
        mod2 = _surface(monkeypatch, home2)
        _, su2 = _call(mod2, count=50)
        assert su2 == []
        assert not (home2 / SIDECAR_NAME).exists()


# ---------------------------------------------------------------------------
# S7 non-regular target
# ---------------------------------------------------------------------------

_FIFO_CHILD = r"""
import importlib.util, json, os, stat, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("hook_fifo_3521r40", sys.argv[2])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
su = []
res = mod.check_principle_triggers_full(["tr_r40"], 5, state_unknown=su)
home = os.environ["ENGRAM_HOME"]
st = os.stat(os.path.join(home, "principle-trigger-state.json"))
print("RESULT:" + json.dumps({
    "res": res, "su": su, "still_fifo": stat.S_ISFIFO(st.st_mode),
    "files": sorted(os.listdir(home)),
}))
"""


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo")
def test_s7_fifo_state_is_unknown_not_moved_not_written(tmp_path):
    home = _home(tmp_path)
    os.mkfifo(str(home / STATE_NAME))
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-c", _FIFO_CHILD, str(HOOK_DIR), str(_SURFACE_PATH)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert line, (proc.stdout, proc.stderr)
    data = json.loads(line[0][len("RESULT:"):])
    assert "r40 claim text" in data["res"][0]  # firing decision unchanged
    assert len(data["su"]) == 1
    assert data["su"][0]["moved_to"] is None
    assert ": " in data["su"][0]["reason"]
    assert data["still_fifo"] is True
    assert SIDECAR_NAME not in data["files"]


# ---------------------------------------------------------------------------
# S8 main() rendering
# ---------------------------------------------------------------------------

def _stub_surface_main(mod, monkeypatch, matched=(TRIG,)):
    monkeypatch.setattr(mod, "query_daemon",
                        lambda *a, **kw: {"matched_ids": list(matched), "match_count": 0, "nodes": []})
    monkeypatch.setattr(mod, "_get_auto_surface_config",
                        lambda: {"idf_gate_enabled": False,
                                 "short_prompt_threshold_chars": 0,
                                 "prev_response_tail_chars": 0})
    monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
    monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: "")
    monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
    monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
    monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
    monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
    monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
    monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))


def _run_main(mod, capsys):
    capsys.readouterr()
    stdin_data = json.dumps({"prompt": "hello"})
    buf = io.StringIO()
    code = None
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit as e:
                code = e.code
    raw = buf.getvalue()
    out = json.loads(raw.strip())  # exactly ONE JSON object, or this raises
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    return code, ctx, raw, capsys.readouterr().err.splitlines()


class TestMainRendering:
    def test_s8_probe_line_stderr_lines_and_rate_limit(self, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)

        code, ctx1, _raw, err1 = _run_main(mod, capsys)
        assert code in (0, None)
        inj = [ln for ln in ctx1.splitlines() if "principle cooldown state UNKNOWN" in ln]
        assert len(inj) == 1, ctx1
        assert inj[0] == (
            "[probe] principle cooldown state UNKNOWN (JSONDecodeError); "
            "cooldown reset; kept as principle-trigger-state.json.corrupt"
        )
        assert len(inj[0]) <= 200
        assert "r40 claim text" in ctx1  # the nudge still fired
        probe_err = [ln for ln in err1 if "principle-trigger-state unknown:" in ln]
        assert len(probe_err) == 1
        assert probe_err[0].startswith(
            "engram-surface-hook: principle-trigger-state unknown: JSONDecodeError:")
        moved_err = [ln for ln in err1 if "unparseable state file moved aside" in ln]
        assert len(moved_err) == 1, err1
        assert moved_err[0].startswith("[engram principle-trigger-state] ")
        assert str(home / SIDECAR_NAME) in moved_err[0]
        assert "JSONDecodeError" in moved_err[0]
        assert "only the latest corrupt copy is kept" in moved_err[0]
        stamp = json.loads((home / STAMP_NAME).read_text())["shown_at"]
        assert "principle-trigger-state" in stamp and "principle-triggers" not in stamp

        # Second run: state is now valid (the file was moved aside and rewritten),
        # so re-corrupt it. Within the window: NO injected line, stderr still there.
        _plant(home, CORRUPT_B)
        _, ctx2, _raw2, err2 = _run_main(mod, capsys)
        assert "principle cooldown state UNKNOWN" not in ctx2
        assert len([ln for ln in err2 if "principle-trigger-state unknown:" in ln]) == 1

    def test_s8_not_moved_wording_when_nothing_fired(self, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch, matched=(TRIG_BOGUS,))
        _, ctx, _raw, err = _run_main(mod, capsys)
        inj = [ln for ln in ctx.splitlines() if "principle cooldown state UNKNOWN" in ln]
        assert inj == [
            "[probe] principle cooldown state UNKNOWN (JSONDecodeError); "
            "cooldowns ignored this prompt"
        ]
        assert not [ln for ln in err if "moved aside" in ln]
        assert (home / STATE_NAME).read_bytes() == CORRUPT_A
        assert not (home / SIDECAR_NAME).exists()

    def test_s8_healthy_and_absent_state_no_notice_no_stamp(self, tmp_path, monkeypatch, capsys):
        for name, data in (("absent", None), ("valid", json.dumps(RICH_STATE).encode())):
            home = _home(tmp_path, name)
            if data is not None:
                _plant(home, data)
            mod = _surface(monkeypatch, home)
            _stub_surface_main(mod, monkeypatch)
            _, ctx, _raw, err = _run_main(mod, capsys)
            assert "state UNKNOWN" not in ctx
            assert not [ln for ln in err if "principle-trigger-state" in ln]
            assert not (home / STAMP_NAME).exists()
            assert not (home / SIDECAR_NAME).exists()

    def test_s8_registry_unknown_keeps_its_own_probe_key(self, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path)
        (home / "principle_triggers.json").write_text("{corrupt")
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _, ctx, _raw, err = _run_main(mod, capsys)
        assert "principle triggers state UNKNOWN" in ctx
        assert [ln for ln in err if "principle-triggers unknown:" in ln]


# ---------------------------------------------------------------------------
# S9 healthy path byte-identical to the merge-base copy
# ---------------------------------------------------------------------------

def _git(*args):
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args],
                          capture_output=True, text=True)


def _merge_base_hook_text():
    if _git("rev-parse", "--verify", "origin/dev").returncode != 0:
        pytest.skip("origin/dev not available in this checkout")
    if _git("merge-base", "--is-ancestor", "HEAD", "origin/dev").returncode == 0:
        pytest.skip("HEAD is already contained in origin/dev: no change to compare")
    mb = _git("merge-base", "origin/dev", "HEAD")
    if mb.returncode != 0:
        pytest.skip("no merge-base with origin/dev")
    show = _git("show", f"{mb.stdout.strip()}:{_REL}")
    if show.returncode != 0:
        pytest.skip("hook not present at merge-base")
    return show.stdout


@pytest.mark.parametrize("state", [None, RICH_STATE], ids=["absent", "valid_dict"])
def test_s9_healthy_path_byte_identical_to_merge_base(state, tmp_path, monkeypatch, capsys):
    dev_text = _merge_base_hook_text()
    dev_file = tmp_path / "dev-surface-hook.py"
    dev_file.write_text(dev_text)
    results = {}
    for label in ("new", "dev"):
        home = _home(tmp_path, label)
        if state is not None:
            _plant(home, json.dumps(state).encode())
        monkeypatch.setenv("ENGRAM_HOME", str(home))
        mod = _load(_SURFACE_PATH if label == "new" else dev_file, f"surface_hook_s9_{label}")
        # function level (count 500 clears the rich state's decayed cooldown)
        fl = mod.check_principle_triggers_full([TRIG], 500)
        after_fn = (home / STATE_NAME).read_bytes() if (home / STATE_NAME).exists() else None
        # main() level on the same home
        _stub_surface_main(mod, monkeypatch)
        _code, ctx, raw, err = _run_main(mod, capsys)
        after_main = (home / STATE_NAME).read_bytes() if (home / STATE_NAME).exists() else None
        results[label] = (fl, after_fn, ctx, raw, err, after_main, _hook_files(home))
    assert results["new"][0][0]  # control: it really rendered
    assert results["new"] == results["dev"]
    assert SIDECAR_NAME not in results["new"][6]


def test_s9_valid_state_in_cooldown_is_byte_unchanged(tmp_path, monkeypatch):
    # Control from the defect report: a valid state inside its cooldown
    # suppresses the nudge and leaves the file byte-unchanged.
    home = _home(tmp_path)
    raw = json.dumps(RICH_STATE).encode()
    _plant(home, raw)
    mod = _surface(monkeypatch, home)
    (text, rendered), su = _call(mod, count=3)  # PID last fired at 1, cooldown 10
    assert (text, rendered) == ("", [])
    assert su == []
    assert (home / STATE_NAME).read_bytes() == raw


# ---------------------------------------------------------------------------
# S10 never raises / never kills the process
# ---------------------------------------------------------------------------

class TestNeverRaises:
    @pytest.mark.parametrize("data", [CORRUPT_A, LIST_BYTES, b"", b"\xff\xfe\x00"])
    def test_in_process_corrupt_state_never_raises(self, data, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, data)
        mod = _surface(monkeypatch, home)
        (text, _r), su = _call(mod)
        assert "r40 claim text" in text
        assert len(su) == 1
        assert (home / SIDECAR_NAME).read_bytes() == data

    def test_move_aside_oserror_is_swallowed(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant(home, CORRUPT_A)
        mod = _surface(monkeypatch, home)

        def boom(*a, **kw):
            raise OSError(5, "synthetic EIO")

        monkeypatch.setattr(mod.os, "replace", boom)
        (text, _r), su = _call(mod)  # must not raise
        assert "r40 claim text" in text
        assert (home / STATE_NAME).read_bytes() == CORRUPT_A

    def test_main_cell_passes_in_a_child_pytest(self, tmp_path):
        if os.environ.get("R40_CHILD_PYTEST"):
            pytest.skip("already inside the child run")
        env = dict(os.environ)
        env["R40_CHILD_PYTEST"] = "1"
        node = (f"tests/{Path(__file__).name}::TestMainRendering::"
                "test_s8_probe_line_stderr_lines_and_rate_limit")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        last = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
        assert "1 passed" in last, proc.stdout + proc.stderr
