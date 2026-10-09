"""Contract-first tests for #3521 row 7: a broken principle-trigger registry
must not look like "nothing fired".

`engram-surface-hook.py::check_principle_triggers_full(matched_ids,
prompt_count, unknown=None)` reads `principle_triggers.json` on every prompt.
Before this row, an unreadable / corrupt / wrong-shaped registry returned
`("", [])` -- byte-identical to "no principle matched" -- so a broken registry
was indistinguishable from a quiet prompt, forever.

The contract (tri-state, same as rows 1-4 of #3521):

  1. ABSENT registry (FileNotFoundError) stays SILENT: `("", [])`, nothing
     appended to `unknown`, no stderr. It has a documented legitimate window
     right after an upgrade.
  2. Every OTHER way of failing to read or use the registry is UNKNOWN:
     corrupt JSON, a non-dict top level, a regular-file OSError (mode 000), a
     non-regular target (a FIFO, #3628), and any exception caught by the outer
     wrapper after a successful read. The function still returns `("", [])`
     (return shape unchanged) and, when `unknown` is a list, appends exactly
     ONE reason `"<ExceptionClass>: <message>"` (non-dict: a fixed
     `"ValueError: registry is not an object"`).
  3. `unknown=None` (the default; the legacy `check_principle_triggers`
     caller) behaves exactly as before: never raises, returns `("", [])`.
  4. main() passes a fresh list; when non-empty it renders ONE stderr line
     every run and ONE injected `[probe] principle triggers state
     UNKNOWN (<Class>); ...` line per rate-limit window, through the existing
     `_probe_unknown_notice` + `probe_stamps_pending` stamping path.
  5. Healthy path is byte-identical to origin/dev (text and rendered list); an
     empty `matched_ids` still returns before touching the registry.

Cell classes: healthy / absent / legacy-None cells are GREEN on unmodified dev
(they pin that nothing else moved); the unknown cells and the main() cells are
RED on dev (no `unknown` kwarg, no notice).
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
_SURFACE_PATH = HOOK_DIR / "engram-surface-hook.py"
_REL = "src/engram/hooks/claude/engram-surface-hook.py"

STAMP_NAME = "probe-unknown-notice.json"
TRIG = "pfix-trig"
PID = "pfix-lesson"

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
    return _load(_SURFACE_PATH, "surface_hook_3521r7")


def _home(tmp_path: Path, name="home") -> Path:
    h = tmp_path / name
    h.mkdir()
    return h


GOOD_REGISTRY = {
    TRIG: [{
        "principle_id": PID,
        "kind": "lesson",
        "claim": "pfix claim text",
        "nudge": "pfix nudge text",
    }]
}


def _write_registry(home: Path, content):
    p = home / "principle_triggers.json"
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    return p


def _call(mod, unknown="list"):
    """Run the function; return (result, unknown_list_or_None)."""
    if unknown == "list":
        u = []
        return mod.check_principle_triggers_full([TRIG], 5, unknown=u), u
    return mod.check_principle_triggers_full([TRIG], 5), None


# ---------------------------------------------------------------------------
# Function level
# ---------------------------------------------------------------------------

class TestHealthyAndAbsent:
    def test_healthy_renders_and_records_nothing(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _write_registry(home, GOOD_REGISTRY)
        mod = _surface(monkeypatch, home)
        (text, rendered), u = _call(mod)
        assert text and PID in text and "pfix claim text" in text
        assert len(rendered) == 1
        assert u == []

    def test_healthy_byte_identical_to_dev(self, tmp_path, monkeypatch):
        r = subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"origin/dev:{_REL}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip("origin/dev not available in this checkout")
        dev_file = tmp_path / "dev-surface-hook.py"
        dev_file.write_text(r.stdout)

        home_new = _home(tmp_path, "new")
        home_dev = _home(tmp_path, "dev")
        _write_registry(home_new, GOOD_REGISTRY)
        _write_registry(home_dev, GOOD_REGISTRY)

        new_mod = _surface(monkeypatch, home_new)
        monkeypatch.setenv("ENGRAM_HOME", str(home_dev))
        dev_mod = _load(dev_file, "surface_hook_dev_3521r7")

        new_out = new_mod.check_principle_triggers_full([TRIG], 5)
        dev_out = dev_mod.check_principle_triggers_full([TRIG], 5)
        assert new_out[0]
        assert new_out == dev_out

    def test_absent_stays_silent(self, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path)
        mod = _surface(monkeypatch, home)
        capsys.readouterr()
        (res, u) = _call(mod)
        assert res == ("", [])
        assert u == []
        assert capsys.readouterr().err == ""

    def test_empty_matched_ids_returns_before_registry(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _write_registry(home, "{corrupt")
        mod = _surface(monkeypatch, home)
        u = []
        assert mod.check_principle_triggers_full([], 5, unknown=u) == ("", [])
        assert u == []

    def test_empty_object_registry_is_silent(self, tmp_path, monkeypatch):
        # `{}` is a valid (empty) registry: nothing to fire, nothing unknown.
        home = _home(tmp_path)
        _write_registry(home, {})
        mod = _surface(monkeypatch, home)
        res, u = _call(mod)
        assert res == ("", []) and u == []


def _plant_corrupt(home):
    _write_registry(home, '{"pfix-trig": [')


def _plant_non_dict(home):
    _write_registry(home, [1, 2, 3])


def _plant_mode000(home):
    p = _write_registry(home, GOOD_REGISTRY)
    p.chmod(0)


def _plant_bad_entry(home):
    # Read succeeds; the by_principle loop raises AttributeError on a str entry.
    _write_registry(home, {TRIG: ["not-an-object"]})


UNKNOWN_CELLS = {
    "corrupt_json": (_plant_corrupt, "JSONDecodeError"),
    "non_dict_list": (_plant_non_dict, "ValueError"),
    "mode_000": (_plant_mode000, "PermissionError"),
    "bad_entry_outer_wrapper": (_plant_bad_entry, "AttributeError"),
}


def _skip_if_root_mode000(cell):
    if cell == "mode_000" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses mode 000")


class TestUnknownCells:
    @pytest.mark.parametrize("cell", sorted(UNKNOWN_CELLS))
    def test_unknown_records_one_reason(self, cell, tmp_path, monkeypatch):
        _skip_if_root_mode000(cell)
        planter, exc_cls = UNKNOWN_CELLS[cell]
        home = _home(tmp_path)
        planter(home)
        mod = _surface(monkeypatch, home)
        try:
            res, u = _call(mod)
        finally:
            (home / "principle_triggers.json").chmod(0o600)
        assert res == ("", [])
        assert len(u) == 1, u
        assert u[0].startswith(exc_cls + ":"), u[0]

    def test_non_dict_reason_is_fixed_text(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant_non_dict(home)
        mod = _surface(monkeypatch, home)
        _, u = _call(mod)
        assert u == ["ValueError: registry is not an object"]

    @pytest.mark.parametrize("raw", ["null", "0", '"str"', "[]"])
    def test_other_non_dict_json_values_are_unknown(self, raw, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _write_registry(home, raw)
        mod = _surface(monkeypatch, home)
        res, u = _call(mod)
        assert res == ("", []) and len(u) == 1

    @pytest.mark.parametrize("cell", sorted(UNKNOWN_CELLS))
    def test_unknown_none_behaves_as_before(self, cell, tmp_path, monkeypatch):
        _skip_if_root_mode000(cell)
        planter, _ = UNKNOWN_CELLS[cell]
        home = _home(tmp_path)
        planter(home)
        mod = _surface(monkeypatch, home)
        try:
            res, u = _call(mod, unknown=None)
        finally:
            (home / "principle_triggers.json").chmod(0o600)
        assert res == ("", []) and u is None

    def test_legacy_wrapper_unchanged(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _plant_corrupt(home)
        mod = _surface(monkeypatch, home)
        assert mod.check_principle_triggers([TRIG], 5) == ""

    def test_outer_wrapper_via_monkeypatched_inner(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        _write_registry(home, GOOD_REGISTRY)
        mod = _surface(monkeypatch, home)

        def boom(*a, **kw):
            raise RuntimeError("synthetic inner failure")

        monkeypatch.setattr(mod, "_check_principle_triggers_inner", boom)
        res, u = _call(mod)
        assert res == ("", [])
        assert u == ["RuntimeError: synthetic inner failure"]
        # and with unknown=None the same failure is still swallowed
        assert mod.check_principle_triggers_full([TRIG], 5) == ("", [])


_FIFO_CHILD = r"""
import importlib.util, json, os, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("hook_fifo_3521r7", sys.argv[2])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
u = []
res = mod.check_principle_triggers_full(["pfix-trig"], 5, unknown=u)
res_none = mod.check_principle_triggers_full(["pfix-trig"], 5)
print("RESULT:" + json.dumps({"res": res, "unknown": u, "res_none": res_none}))
"""


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo")
def test_fifo_registry_is_unknown_and_does_not_hang(tmp_path):
    home = _home(tmp_path)
    os.mkfifo(str(home / "principle_triggers.json"))
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-c", _FIFO_CHILD, str(HOOK_DIR), str(_SURFACE_PATH)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert line, (proc.stdout, proc.stderr)
    data = json.loads(line[0][len("RESULT:"):])
    assert data["res"] == ["", []]
    assert data["res_none"] == ["", []]
    assert len(data["unknown"]) == 1
    cls = data["unknown"][0].split(":", 1)[0]
    assert cls.endswith("Error"), data["unknown"]


# ---------------------------------------------------------------------------
# main() level
# ---------------------------------------------------------------------------

def _stub_surface_main(mod, monkeypatch):
    """Same stubbing as test_probe_unknown_state_3521.py, except the real
    check_principle_triggers_full runs and the daemon reports a matched id."""
    monkeypatch.setattr(mod, "query_daemon",
                        lambda *a, **kw: {"matched_ids": [TRIG], "match_count": 0, "nodes": []})
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
    out = json.loads(buf.getvalue().strip())
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    return code, ctx, capsys.readouterr().err.splitlines()


def _principle_unknown_lines(lines):
    return [ln for ln in lines if "principle-triggers unknown:" in ln]


class TestMainRendering:
    def test_corrupt_registry_renders_one_line_then_rate_limits(self, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path)
        _plant_corrupt(home)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)

        code, ctx1, err1 = _run_main(mod, capsys)
        assert code in (0, None)
        inj = [ln for ln in ctx1.splitlines() if "principle triggers state UNKNOWN" in ln]
        assert len(inj) == 1
        assert inj[0].startswith("[probe] principle triggers state UNKNOWN (JSONDecodeError); ")
        assert "trigger nudges skipped this prompt" in inj[0]
        assert len(inj[0]) <= 200
        u1 = _principle_unknown_lines(err1)
        assert len(u1) == 1
        assert u1[0].startswith("engram-surface-hook: principle-triggers unknown: JSONDecodeError:")
        assert (home / STAMP_NAME).exists()

        _, ctx2, err2 = _run_main(mod, capsys)
        assert "principle triggers state UNKNOWN" not in ctx2
        assert len(_principle_unknown_lines(err2)) == 1

    def test_healthy_registry_no_notice_no_stamp(self, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path)
        _write_registry(home, GOOD_REGISTRY)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _, ctx, err = _run_main(mod, capsys)
        assert "pfix claim text" in ctx  # control: the matched trigger really rendered
        assert "state UNKNOWN" not in ctx
        assert _principle_unknown_lines(err) == []
        assert not (home / STAMP_NAME).exists()

    def test_absent_registry_no_notice_no_stamp(self, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path)
        mod = _surface(monkeypatch, home)
        _stub_surface_main(mod, monkeypatch)
        _, ctx, err = _run_main(mod, capsys)
        assert "state UNKNOWN" not in ctx
        assert _principle_unknown_lines(err) == []
        assert not (home / STAMP_NAME).exists()
