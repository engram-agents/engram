"""Contract-first tests for #3521 row 8: the drowsiness banner must say
"unknown" when the transcript could not be read.

The defect: ``context_tracker.estimate_usage`` returned ``None`` (so no banner
at all) when the transcript path resolved but could not be opened (mode 000, a
FIFO, EIO). That silences the pre-compaction save timer exactly when it is
needed, and a silent banner reads as "all is well".

The contract (maintainer ruling). ``read_current_tokens`` has four ``None``
paths. THREE are legitimate and stay byte-identical to dev:

  1. ``_resolve_jsonl`` returns no path (older Claude Code, sub-agent and
     headless contexts);
  2. the post-compact window holds no usage record (the marker offset, or a
     legacy ``compact_boundary``);
  3. there is no usage record yet.

The FOURTH becomes UNKNOWN: the ``except OSError`` around
``open_regular_text(jsonl_path)`` on a RESOLVED path (mode 000, a FIFO or other
non-regular file, EIO).

  * ``estimate_usage`` returns ``{"unknown": "<Type>: <msg>"}`` for that one
    case. ``read_current_tokens`` keeps its public behaviour: still ``None``.
  * ``format_drowsiness`` branches on ``"unknown" in usage`` FIRST (the unknown
    dict never reaches the percentage arithmetic) and renders
    ``[Drowsiness: unknown (<Type>)]`` -- the class only, never the raw
    message, one bounded line. It also writes ONE stderr line through
    ``_hooklib.write_probe_unknown_stderr``.
  * NO rate limit (maintainer ruling, round 2): the healthy banner already
    renders on every prompt, so an unknown banner on every prompt adds no
    noise, and with no stamp there is no "stamped at render, then the line is
    dropped under the injection budget" race. The line and the stderr line
    appear on EVERY call; no probe stamp file is created or read.
  * A transcript that ``_resolve_jsonl`` resolved but that vanishes before the
    open (``FileNotFoundError``) is UNKNOWN by design: resolved, then
    unreadable.
  * The healthy dict renders byte-identical to dev for every band, and the
    callers (engram-surface-hook.py, context_tracker_hook.py) are unchanged:
    nothing here may raise through them.

Cell classes (the dev red/green record is in the PR handoff):
  * healthy / legitimate-None cells -- GREEN on unmodified dev (they pin that
    the fix does not disturb it);
  * unknown / stderr / no-rate-limit / end-to-end cells -- RED on unmodified dev.
"""

import errno
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
_CT_PATH = HOOK_DIR / "context_tracker.py"
_CT_HOOK_PATH = HOOK_DIR / "context_tracker_hook.py"
_SURFACE_PATH = HOOK_DIR / "engram-surface-hook.py"
_STAMP = "probe-unknown-notice.json"

_COUNTER = 0
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _load_ct(path: Path, home: Path, monkeypatch):
    """Load a context_tracker.py by path against a scratch ENGRAM_HOME."""
    global _COUNTER
    _COUNTER += 1
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"ct_3521_{_COUNTER}", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dev_ct(tmp_path: Path, home: Path, monkeypatch):
    """Dev's own context_tracker.py (git show origin/dev) with dev's _hooklib
    beside it, loaded against the same scratch ENGRAM_HOME."""
    d = tmp_path / "devct"
    d.mkdir(exist_ok=True)
    for name in ("context_tracker.py", "_hooklib.py"):
        r = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "show",
             f"origin/dev:src/engram/hooks/claude/{name}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            pytest.skip(f"origin/dev not available for the byte-identity cells: {r.stderr[:120]}")
        (d / name).write_text(r.stdout)
    return _load_ct(d / "context_tracker.py", home, monkeypatch)


def _usage_line(inp=0, read=0, create=0) -> str:
    return json.dumps({"message": {"usage": {
        "input_tokens": inp, "cache_read_input_tokens": read,
        "cache_creation_input_tokens": create}}}) + "\n"


def _config(home: Path, ceiling: int) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(
        {"cadence": {"drowsiness_ceiling_tokens": ceiling}}))


def _home(tmp_path: Path, ceiling: int = 1_000_000) -> Path:
    home = tmp_path / "engram_home"
    _config(home, ceiling)
    return home


def _transcript(tmp_path: Path, tokens: int = 100_000) -> Path:
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(inp=tokens))
    return t


def _mode000(tmp_path: Path) -> Path:
    if IS_ROOT:
        pytest.skip("root bypasses mode 000")
    t = _transcript(tmp_path)
    t.chmod(0)
    return t


def _fifo(tmp_path: Path) -> Path:
    p = tmp_path / "fifo.jsonl"
    os.mkfifo(p)
    return p


def _stderr_lines(capsys) -> list:
    err = capsys.readouterr().err
    return [ln for ln in err.splitlines() if ln.strip()]


def _unknown_type(usage) -> str:
    return usage["unknown"].split(":", 1)[0]


# --------------------------------------------------------------------------
# healthy dict: byte-identical to dev for every band
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tokens,ceiling,literal_prefix", [
    (100_000, 1_000_000, "[Drowsiness: refreshed]"),                 # refreshed
    (600_000, 1_000_000, "[Drowsiness: energetic]"),                 # energetic
    (850_000, 1_000_000, "[Drowsiness: a little drowsy]"),           # drowsy block
    (950_000, 1_000_000, "[Drowsiness: needs a nap: 95%]"),          # urgent
    (140_000, 152_000, "[Drowsiness: needs a nap: 92%]"),            # 200k mode urgent
])
def test_healthy_dict_and_banner_identical_to_dev(tmp_path, monkeypatch, capsys,
                                                  tokens, ceiling, literal_prefix):
    home = _home(tmp_path, ceiling)
    t = _transcript(tmp_path, tokens)
    new = _load_ct(_CT_PATH, home, monkeypatch)
    dev = _dev_ct(tmp_path, home, monkeypatch)
    u_new = new.estimate_usage(transcript_path=str(t))
    u_dev = dev.estimate_usage(transcript_path=str(t))
    assert u_new == u_dev and "unknown" not in u_new
    out_new = new.format_drowsiness(u_new)
    assert out_new == dev.format_drowsiness(u_dev)
    assert out_new.startswith(literal_prefix)
    assert not (home / _STAMP).exists(), "healthy path must not touch the probe stamp"
    assert "unknown" not in capsys.readouterr().err


def test_healthy_loop_mode_banner_identical_to_dev(tmp_path, monkeypatch):
    home = _home(tmp_path)
    (home / "loop-mode.json").write_text("{}")
    t = _transcript(tmp_path, 950_000)
    new = _load_ct(_CT_PATH, home, monkeypatch)
    dev = _dev_ct(tmp_path, home, monkeypatch)
    # alpha #3863 (6b): the banner's history path follows ENGRAM_HOME; dev spelled it ~/.engram
    assert new.format_drowsiness(new.estimate_usage(transcript_path=str(t))) == \
        dev.format_drowsiness(dev.estimate_usage(transcript_path=str(t))).replace("~/.engram", str(home))


# --------------------------------------------------------------------------
# the three legitimate None paths stay None, silent
# --------------------------------------------------------------------------

def _legit_none_fixtures(tmp_path: Path, home: Path):
    """name -> (transcript_path arg, session_id arg), built under tmp_path."""
    out = {"unresolvable": (None, None)}
    out["missing_path"] = (str(tmp_path / "nope.jsonl"), None)

    eof = tmp_path / "eof.jsonl"
    eof.write_text(_usage_line(inp=500_000))
    (home / "last-compact-at.json").write_text(json.dumps(
        {"jsonl_path": str(eof), "byte_offset": eof.stat().st_size}))
    out["compact_marker_at_eof"] = (str(eof), None)

    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(_usage_line(inp=500_000) + '{"type":"compact_boundary"}\n')
    out["legacy_compact_boundary"] = (str(legacy), None)

    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"type":"user"}\n{"message":{"role":"assistant"}}\n')
    out["no_usage_record"] = (str(empty), None)
    return out


LEGIT = ["unresolvable", "missing_path", "compact_marker_at_eof",
         "legacy_compact_boundary", "no_usage_record"]


@pytest.mark.parametrize("name", LEGIT)
def test_legitimate_none_paths_stay_none_and_silent(tmp_path, monkeypatch, capsys, name):
    home = _home(tmp_path)
    fx = _legit_none_fixtures(tmp_path, home)
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    tp, sid = fx[name]
    assert ct.estimate_usage(transcript_path=tp, session_id=sid) is None
    assert ct.read_current_tokens(tp) is None
    assert not (home / _STAMP).exists()
    assert _stderr_lines(capsys) == []


@pytest.mark.parametrize("name", ["unresolvable", "compact_marker_at_eof", "no_usage_record"])
def test_legitimate_none_paths_hook_prints_nothing(tmp_path, name):
    home = _home(tmp_path)
    tp, _ = _legit_none_fixtures(tmp_path, home)[name]
    r = _run_ct_hook(home, tmp_path, tp)
    assert r.stdout == "" and r.stderr == ""


# --------------------------------------------------------------------------
# unknown: mode 000 / FIFO / EIO / directory
# --------------------------------------------------------------------------

def _bad_fixture(kind: str, tmp_path: Path, ct, monkeypatch) -> str:
    """Return the transcript path for a bad-path fixture; EIO is simulated by
    making the helper raise on the resolved path."""
    if kind == "mode000":
        return str(_mode000(tmp_path))
    if kind == "fifo":
        return str(_fifo(tmp_path))
    if kind == "directory":
        d = tmp_path / "adir.jsonl"
        d.mkdir()
        return str(d)
    if kind == "eio":
        t = _transcript(tmp_path)
        orig = ct.open_regular_text

        def _boom(path, *a, **kw):
            if os.fspath(path) == str(t):
                raise OSError(errno.EIO, "Input/output error", str(path))
            return orig(path, *a, **kw)
        monkeypatch.setattr(ct, "open_regular_text", _boom)
        return str(t)
    raise AssertionError(kind)


BAD = ["mode000", "fifo", "directory", "eio"]


@pytest.mark.parametrize("kind", BAD)
def test_unknown_usage_dict_and_public_none(tmp_path, monkeypatch, kind):
    home = _home(tmp_path)
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    tp = _bad_fixture(kind, tmp_path, ct, monkeypatch)
    usage = ct.estimate_usage(transcript_path=tp)
    assert set(usage) == {"unknown"}
    typ, _, msg = usage["unknown"].partition(": ")
    assert typ.isidentifier() and msg
    assert typ in ("PermissionError", "OSError", "IsADirectoryError")
    # public read_current_tokens behaviour is unchanged
    assert ct.read_current_tokens(tp) is None


@pytest.mark.parametrize("kind", BAD)
def test_unknown_banner_class_only_one_stderr_line(tmp_path, monkeypatch, capsys, kind):
    home = _home(tmp_path)
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    tp = _bad_fixture(kind, tmp_path, ct, monkeypatch)
    usage = ct.estimate_usage(transcript_path=tp)
    capsys.readouterr()
    typ = _unknown_type(usage)
    msg = usage["unknown"].split(": ", 1)[1]
    out = ct.format_drowsiness(usage)
    assert out == f"[Drowsiness: unknown ({typ})]"
    assert "\n" not in out and msg not in out
    lines = _stderr_lines(capsys)
    assert len(lines) == 1
    assert lines[0].startswith(f"context_tracker: drowsiness unknown: {typ}: ")
    assert not (home / _STAMP).exists(), "no rate limit: rendering writes no stamp"


def test_unknown_every_call_shows_line_and_stderr_no_stamp(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path)
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    usage = {"unknown": "PermissionError: [Errno 13] Permission denied: '/x'"}
    for n in range(1, 4):
        assert ct.format_drowsiness(usage) == "[Drowsiness: unknown (PermissionError)]"
        assert len(_stderr_lines(capsys)) == 1, f"call {n}"
    assert not (home / _STAMP).exists()


def test_unknown_ignores_a_fresh_pre_existing_stamp_and_leaves_it_alone(tmp_path, monkeypatch, capsys):
    """A fresh stamp for "drowsiness" (what the old code left) must not
    suppress the line, and the stamp file must not be rewritten."""
    home = _home(tmp_path)
    stamp = json.dumps({"shown_at": {"drowsiness": time.time()}})
    (home / _STAMP).write_text(stamp)
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    usage = {"unknown": "OSError: [Errno 5] Input/output error"}
    assert ct.format_drowsiness(usage) == "[Drowsiness: unknown (OSError)]"
    assert (home / _STAMP).read_text() == stamp


def test_unknown_with_directory_at_stamp_path_still_shows_and_creates_no_stamp(tmp_path, monkeypatch, capsys):
    """Turned from the old unwritable-stamp cell: a directory at the stamp path
    is never touched, and the line shows every time."""
    home = _home(tmp_path)
    (home / _STAMP).mkdir()
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    usage = {"unknown": "OSError: [Errno 5] Input/output error"}
    for _ in range(3):
        assert ct.format_drowsiness(usage) == "[Drowsiness: unknown (OSError)]"
    assert (home / _STAMP).is_dir() and list((home / _STAMP).iterdir()) == []
    assert len(_stderr_lines(capsys)) == 3


def test_resolved_then_vanished_transcript_is_unknown(tmp_path, monkeypatch, capsys):
    """Race: _resolve_jsonl returned a path (it existed) but it is gone by the
    open -> FileNotFoundError -> UNKNOWN by design, distinct from the
    legitimate unresolved-path None."""
    home = _home(tmp_path)
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    gone = str(tmp_path / "vanished.jsonl")
    monkeypatch.setattr(ct, "_resolve_jsonl", lambda *a, **kw: gone)
    usage = ct.estimate_usage(transcript_path=gone)
    assert set(usage) == {"unknown"} and usage["unknown"].startswith("FileNotFoundError: ")
    assert ct.read_current_tokens(gone) is None
    capsys.readouterr()
    assert ct.format_drowsiness(usage) == "[Drowsiness: unknown (FileNotFoundError)]"
    assert ct.format_drowsiness(usage) == "[Drowsiness: unknown (FileNotFoundError)]"
    assert not (home / _STAMP).exists()


def test_unknown_branch_comes_first_even_in_loop_mode_and_with_hostile_text(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path)
    (home / "loop-mode.json").write_text("{}")
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    out = ct.format_drowsiness({"unknown": "OSError: bad\nnewline \x1b[31mred"})
    assert out == "[Drowsiness: unknown (OSError)]"
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and "\x1b" not in lines[0]


def test_unknown_creates_no_stamp_file_anywhere(tmp_path, monkeypatch):
    home = _home(tmp_path)
    before = set(os.listdir(home))
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    ct.format_drowsiness({"unknown": "OSError: x"})
    assert set(os.listdir(home)) == before


# --------------------------------------------------------------------------
# FIFO through a subprocess with a hard timeout (no hang)
# --------------------------------------------------------------------------

_SUB = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("ct_sub", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
u = m.estimate_usage(transcript_path=sys.argv[2])
b1 = m.format_drowsiness(u)
b2 = m.format_drowsiness(u)
print(json.dumps({"usage": u, "b1": b1, "b2": b2}))
"""


def _env(home: Path, tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ENGRAM")}
    env.update({"ENGRAM_HOME": str(home), "HOME": str(tmp_path / "fakehome")})
    (tmp_path / "fakehome").mkdir(exist_ok=True)
    return env


def test_fifo_transcript_subprocess_no_hang_and_unknown(tmp_path):
    home = _home(tmp_path)
    fifo = _fifo(tmp_path)
    r = subprocess.run([sys.executable, "-c", _SUB, str(_CT_PATH), str(fifo)],
                       capture_output=True, text=True, timeout=30, env=_env(home, tmp_path))
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    typ = res["usage"]["unknown"].split(":", 1)[0]
    assert res["b1"] == f"[Drowsiness: unknown ({typ})]"
    assert res["b2"] == res["b1"]
    assert len([ln for ln in r.stderr.splitlines() if ln.strip()]) == 2


# --------------------------------------------------------------------------
# callers end to end
# --------------------------------------------------------------------------

def _run_ct_hook(home: Path, tmp_path: Path, transcript, session_id="s-3521"):
    payload = {"session_id": session_id}
    if transcript:
        payload["transcript_path"] = transcript
    return subprocess.run([sys.executable, str(_CT_HOOK_PATH)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30, env=_env(home, tmp_path))


def test_ct_hook_end_to_end_mode000(tmp_path):
    home = _home(tmp_path)
    t = _mode000(tmp_path)
    r = _run_ct_hook(home, tmp_path, str(t))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[Drowsiness: unknown (PermissionError)]"
    assert len([ln for ln in r.stderr.splitlines() if ln.strip()]) == 1
    r2 = _run_ct_hook(home, tmp_path, str(t))  # no rate limit: same banner again
    assert r2.returncode == 0 and r2.stdout.strip() == "[Drowsiness: unknown (PermissionError)]"
    assert len([ln for ln in r2.stderr.splitlines() if ln.strip()]) == 1


def test_ct_hook_end_to_end_fifo(tmp_path):
    home = _home(tmp_path)
    r = _run_ct_hook(home, tmp_path, str(_fifo(tmp_path)))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("[Drowsiness: unknown (")


def test_ct_hook_healthy_unchanged(tmp_path):
    home = _home(tmp_path)
    r = _run_ct_hook(home, tmp_path, str(_transcript(tmp_path, 100_000)))
    assert r.stdout.strip() == "[Drowsiness: refreshed]" and r.stderr == ""


def _load_surface(home: Path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    sys.modules.pop("engram_surface_hook_3521", None)
    spec = importlib.util.spec_from_file_location("engram_surface_hook_3521", str(_SURFACE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _quiet_surface(mod, monkeypatch):
    """Everything except the drowsiness block stays quiet (same stub set as
    tests/test_surface_hook_injection_bounds.py); context_tracker is REAL."""
    monkeypatch.setattr(mod, "query_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "query_fts_fallback",
                        lambda *a, **kw: {"matched_ids": [], "match_count": 0, "nodes": []})
    monkeypatch.setattr(mod, "_get_auto_surface_config", lambda: {
        "idf_gate_enabled": False, "short_prompt_threshold_chars": 0,
        "prev_response_tail_chars": 0})
    monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: ("", []))
    monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
    monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: "")
    monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
    monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
    monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
    monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
    monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
    monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))
    monkeypatch.setattr(mod, "_check_db_liveness", lambda *a: (True, "ok"))
    monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")


def _run_surface_main(mod, transcript: str) -> str:
    stdin = json.dumps({"prompt": "a prompt", "session_id": "s-3521", "transcript_path": transcript})
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()))):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit:
                pass
    return buf.getvalue()


def test_surface_hook_end_to_end_mode000_shows_unknown_and_raises_nothing(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path)
    t = _mode000(tmp_path)
    mod = _load_surface(home, monkeypatch)
    _quiet_surface(mod, monkeypatch)
    sys.modules.pop("context_tracker", None)  # conftest restores it after the test
    out = _run_surface_main(mod, str(t))
    ctx = json.loads(out.strip())["hookSpecificOutput"]["additionalContext"]
    assert "[Drowsiness: unknown (PermissionError)]" in ctx


def test_surface_hook_healthy_still_refreshed(tmp_path, monkeypatch):
    home = _home(tmp_path)
    t = _transcript(tmp_path, 100_000)
    mod = _load_surface(home, monkeypatch)
    _quiet_surface(mod, monkeypatch)
    sys.modules.pop("context_tracker", None)
    ctx = json.loads(_run_surface_main(mod, str(t)).strip())["hookSpecificOutput"]["additionalContext"]
    assert "[Drowsiness: refreshed]" in ctx and "unknown" not in ctx


# --------------------------------------------------------------------------
# no-raise pin (the surface hook's try catches ImportError only)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", BAD + LEGIT)
def test_no_raise_on_any_bad_fixture(tmp_path, monkeypatch, kind):
    home = _home(tmp_path)
    ct = _load_ct(_CT_PATH, home, monkeypatch)
    if kind in BAD:
        tp, sid = _bad_fixture(kind, tmp_path, ct, monkeypatch), None
    else:
        tp, sid = _legit_none_fixtures(tmp_path, home)[kind]
    usage = ct.estimate_usage(transcript_path=tp, session_id=sid)
    if usage:
        assert isinstance(ct.format_drowsiness(usage), str)
        assert isinstance(ct.format_drowsiness(usage), str)
    else:
        assert kind in LEGIT
