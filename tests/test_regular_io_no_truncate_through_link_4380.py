"""#4380: no write helper truncates or appends through a planted link.

`open_regular_text_write` / `open_regular_text_append` (two AST-identical copies:
core `engram_regular_io.py` and the self-contained `hooks/claude/_hooklib.py`)
used to open with O_TRUNC and no O_NOFOLLOW. open(2) truncated a symlink's
target, or a hard-linked file, BEFORE the fstat check ran, so the check was a
report, not a guard. They now go through the strict open (`_open_single_regular`:
O_NOFOLLOW, never O_TRUNC at open, st_nlink == 1) and truncate on the validated
descriptor.

Every cell that plants a link asserts the TARGET'S BYTES ARE UNCHANGED, not just
that an error was raised: a path that raises AFTER truncating would satisfy the
second and is the bug's shape.

Groups:
  (1) the two helpers x two copies: symlink, dangling symlink, hard link, FIFO,
      regular file, new file mode, truncate-after-validation;
  (2) the tmp-and-rename hook sites (forum cursor/cache/publish-state, inter-agent
      cursor): a link planted at the deterministic `<dest>.tmp` name leaves its
      target untouched and the final file is not replaced;
  (3) the refusal channel (Kepler's contract): the time-bar hook through its real
      `main()`: nothing written, the path named, said once, exit and stdout
      unchanged;
  (4) the direct writers (quote_quality_survey, s2_clause_sheet) refuse links;
  (7) round 2: the loop-arm Stop hook's reply-gate log (append AND rotation path),
      backup_remote_repair's status tmp, and the surface hook's no-_hooklib fallback
      branches.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC / "hooks" / "claude"
TOOLS_DIR = SRC / "tools"

GUARD_S = int(os.environ.get("NO_TRUNCATE_4380_GUARD_S", "8"))
IMPLS = ["_hooklib", "engram_regular_io"]
KINDS = ["write", "append"]

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

_NEEDS_FIFO = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")
_NEEDS_LINKS = pytest.mark.skipif(
    not hasattr(os, "symlink") or not hasattr(os, "link"), reason="no symlink/link support")

PAYLOAD = b"P" * 4096  # the protected target: big enough that a truncation is unmistakable


def _load(impl: str):
    path = (HOOK_DIR / "_hooklib.py") if impl == "_hooklib" else (SRC / "engram_regular_io.py")
    for p in (str(HOOK_DIR), str(SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(f"{impl}_nt4380", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _seat_home(tmp_path, monkeypatch):
    """The refusal notice writes a stamp under $ENGRAM_HOME: keep it in tmp."""
    home = tmp_path / "seat"
    home.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return home


def _opener(mod, kind):
    return mod.open_regular_text_write if kind == "write" else mod.open_regular_text_append


def _is_not_regular(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and type(exc).__name__ == "NotRegularFile"


def _plant(tmp_path, how):
    """A protected `target` (PAYLOAD) and a `victim` path that reaches it by `how`."""
    target = tmp_path / "target.bin"
    target.write_bytes(PAYLOAD)
    victim = tmp_path / "victim.txt"
    if how == "symlink":
        os.symlink(target, victim)
    elif how == "hardlink":
        os.link(target, victim)
    else:  # pragma: no cover
        raise AssertionError(how)
    return target, victim


# ------------------------------------------------------------------ (1) the helpers

@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("impl", IMPLS)
def test_planted_link_is_refused_and_target_bytes_are_unchanged(tmp_path, impl, kind, how):
    target, victim = _plant(tmp_path, how)
    mod = _load(impl)
    with pytest.raises(OSError) as ei:
        with _opener(mod, kind)(victim, encoding="utf-8") as f:
            f.write("clobber")
    assert _is_not_regular(ei.value), repr(ei.value)
    assert target.read_bytes() == PAYLOAD  # not truncated, not appended to
    assert str(victim) in str(ei.value)


@_NEEDS_LINKS
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("impl", IMPLS)
def test_dangling_symlink_is_refused_and_creates_nothing(tmp_path, impl, kind):
    ghost = tmp_path / "ghost.bin"
    victim = tmp_path / "victim.txt"
    os.symlink(ghost, victim)
    mod = _load(impl)
    with pytest.raises(OSError) as ei:
        _opener(mod, kind)(victim).close()
    assert _is_not_regular(ei.value), repr(ei.value)
    assert not ghost.exists()  # O_CREAT must not create the link's target


@_NEEDS_FIFO
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("impl", IMPLS)
def test_fifo_without_reader_still_fails_at_once(tmp_path, impl, kind):
    os.mkfifo(tmp_path / "f")
    code = f"""
import importlib.util, sys
sys.path.insert(0, {str(HOOK_DIR)!r}); sys.path.insert(0, {str(SRC)!r})
path = {str((HOOK_DIR / '_hooklib.py') if impl == '_hooklib' else (SRC / 'engram_regular_io.py'))!r}
spec = importlib.util.spec_from_file_location('m4380', path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
fn = mod.open_regular_text_{kind}
try:
    fn({str(tmp_path / 'f')!r}).close(); print('no-error')
except OSError as e:
    print('refused', type(e).__name__)
"""
    env = dict(os.environ)
    try:
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=GUARD_S, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{impl}.open_regular_text_{kind} blocked on a reader-less FIFO")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("refused"), done.stdout


@pytest.mark.parametrize("impl", IMPLS)
def test_regular_file_write_replaces_all_content_only_after_validation(tmp_path, impl):
    p = tmp_path / "r.txt"
    p.write_bytes(b"x" * 100)
    mod = _load(impl)
    with mod.open_regular_text_write(p, encoding="utf-8") as f:
        f.write("short")
    assert p.read_bytes() == b"short"


@pytest.mark.parametrize("impl", IMPLS)
def test_regular_file_append_keeps_content(tmp_path, impl):
    p = tmp_path / "r.txt"
    p.write_bytes(b"one\n")
    mod = _load(impl)
    with mod.open_regular_text_append(p, encoding="utf-8") as f:
        f.write("two\n")
    assert p.read_bytes() == b"one\ntwo\n"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("impl", IMPLS)
def test_new_file_is_created_with_0666_less_umask(tmp_path, impl, kind):
    mod = _load(impl)
    old = os.umask(0o027)
    try:
        p = tmp_path / "new.txt"
        with _opener(mod, kind)(p, encoding="utf-8") as f:
            f.write("hi")
    finally:
        os.umask(old)
    assert (p.stat().st_mode & 0o777) == 0o640
    assert p.read_text() == "hi"


@pytest.mark.parametrize("impl", IMPLS)
def test_directory_is_refused_as_not_regular(tmp_path, impl):
    (tmp_path / "d").mkdir()
    mod = _load(impl)
    with pytest.raises(OSError) as ei:
        mod.open_regular_text_write(tmp_path / "d").close()
    assert _is_not_regular(ei.value), repr(ei.value)


# ------------------------------------------------------------------ (2) tmp-and-rename sites

_CHILD = r"""
import importlib.util, json, os, sys
spec_in = json.loads(sys.argv[1])
sys.path.insert(0, os.path.dirname(spec_in["hook"]))
spec = importlib.util.spec_from_file_location("hook_4380_child", spec_in["hook"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = spec_in["paths"]
ns = {"mod": mod, "P": P, "os": os, "json": json, "result": None}
exec(spec_in["code"], ns)
print("RESULT:" + json.dumps(ns["result"]))
"""


def _run_hook_child(tmp_path, hook_file, code):
    home = tmp_path / "seat"
    env = dict(os.environ)
    for k in ("ENGRAM_RUNTIME_DIR", "CLAUDE_PLUGIN_ROOT", "CODEX_PLUGIN_ROOT", "PYTHONPATH"):
        env.pop(k, None)
    env.update({"HOME": str(tmp_path / "home"), "ENGRAM_HOME": str(home),
                "ENGRAM_SESSION_ROLE": "interactive"})
    payload = json.dumps({"hook": str(HOOK_DIR / hook_file),
                          "paths": {"eh": str(home)}, "code": code})
    done = subprocess.run([sys.executable, "-c", _CHILD, payload], capture_output=True,
                          text=True, timeout=GUARD_S, env=env)
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):]), done.stderr


# (hook file, final name, code that writes it, content marker expected on success)
_TMP_SITES = {
    "forum_write_cursor": (
        "engram-forum-prompt-hook.py", "cur.txt",
        "from datetime import datetime, timezone\n"
        "mod._write_cursor(P['eh'] + '/cur.txt', datetime(2026, 1, 2, tzinfo=timezone.utc))\n",
        "2026-01-02"),
    "forum_save_cache": (
        "engram-forum-prompt-hook.py", "hc.json",
        "mod.HOOK_CACHE_PATH = P['eh'] + '/hc.json'\nmod._save_cache([], [])\n",
        "fetched_at"),
    "forum_save_publish_state": (
        "engram-forum-prompt-hook.py", "pub.json",
        "mod.STATUS_PUBLISH_PATH = P['eh'] + '/pub.json'\n"
        "mod._save_publish_state('idle', None, [], None, 1.0)\n",
        "idle"),
    "inter_agent_write_cursor": (
        "engram-inter-agent-prompt-hook.py", "cur.txt",
        "mod._write_cursor_int(P['eh'] + '/cur.txt', 7)\n",
        "7"),
}


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
@pytest.mark.parametrize("site", sorted(_TMP_SITES))
def test_link_planted_at_the_deterministic_tmp_name_leaves_target_unchanged(tmp_path, site, how):
    hook_file, final_name, code, _marker = _TMP_SITES[site]
    home = tmp_path / "seat"
    final = home / final_name
    final.write_text("GOOD-OLD-CONTENT")
    target = tmp_path / "protected.bin"
    target.write_bytes(PAYLOAD)
    tmp_name = home / (final_name + ".tmp")
    if how == "symlink":
        os.symlink(target, tmp_name)
    else:
        os.link(target, tmp_name)
    _run_hook_child(tmp_path, hook_file, code + "result = 1")
    assert target.read_bytes() == PAYLOAD  # the link target was never opened for write
    assert final.read_text() == "GOOD-OLD-CONTENT"  # and the final file was not replaced


@pytest.mark.parametrize("site", sorted(_TMP_SITES))
def test_clean_tmp_name_still_writes_and_replaces(tmp_path, site):
    """Positive control: with no plant the same entry point does write the file."""
    hook_file, final_name, code, marker = _TMP_SITES[site]
    final = tmp_path / "seat" / final_name
    final.write_text("GOOD-OLD-CONTENT")
    _run_hook_child(tmp_path, hook_file, code + "result = 1")
    assert marker in final.read_text()


# ------------------------------------------------------------------ (3) the refusal channel

def _load_time_bar():
    spec = importlib.util.spec_from_file_location("time_bar_4380", HOOK_DIR / "engram-time-bar-hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _typed_payload():
    return {"session_id": "s4380", "promptSource": "typed", "prompt": "hello there"}


def _run_main(tbh, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tbh, "LAST_USER_MSG", tmp_path / "last-user-msg.json")
    monkeypatch.setattr(tbh, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(tbh, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_typed_payload())))
    rc = None
    try:
        tbh.main()
    except SystemExit as e:  # a hook that exits must exit 0
        rc = e.code
    out = capsys.readouterr()
    return rc, out.out, out.err


def _strip_clock(stdout: str) -> dict:
    d = json.loads(stdout)
    ctx = d["hookSpecificOutput"]["additionalContext"]
    return {"event": d["hookSpecificOutput"]["hookEventName"], "shape": sorted(d),
            "prefix": ctx.split(" ")[0], "has_time": ctx.startswith("[Time:")}


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
def test_time_bar_refusal_writes_nothing_names_the_path_says_once_and_proceeds(
        tmp_path, monkeypatch, capsys, how, _seat_home):
    tbh = _load_time_bar()
    activity = tmp_path / "last-user-activity"
    target = tmp_path / "protected.bin"
    target.write_bytes(PAYLOAD)
    if how == "symlink":
        os.symlink(target, activity)
    else:
        os.link(target, activity)
    monkeypatch.setattr(tbh, "LAST_USER_ACTIVITY", activity)

    # Baseline: the same entry point with a clean path, for the stdout contract.
    clean_activity = tmp_path / "clean-activity"
    monkeypatch.setattr(tbh, "LAST_USER_ACTIVITY", clean_activity)
    rc0, out0, _err0 = _run_main(tbh, tmp_path, monkeypatch, capsys)
    (tmp_path / "last-user-msg.json").unlink(missing_ok=True)
    monkeypatch.setattr(tbh, "LAST_USER_ACTIVITY", activity)

    rc1, out1, err1 = _run_main(tbh, tmp_path, monkeypatch, capsys)
    assert target.read_bytes() == PAYLOAD                       # (a) nothing written
    assert str(activity) in err1                                # (b) names the path ...
    assert "regular file" in err1                               #     ... and the fix
    assert ("symlink" if how == "symlink" else "hard link") in err1
    assert rc1 == rc0                                           # (c) exit unchanged
    assert _strip_clock(out1) == _strip_clock(out0)             #     stdout contract unchanged
    assert "Traceback" not in err1

    (tmp_path / "last-user-msg.json").unlink(missing_ok=True)
    rc2, out2, err2 = _run_main(tbh, tmp_path, monkeypatch, capsys)
    assert str(activity) not in err2                            # (b) once, not every prompt
    assert rc2 == rc0 and _strip_clock(out2) == _strip_clock(out0)
    assert target.read_bytes() == PAYLOAD


@_NEEDS_LINKS
def test_refusal_notice_marker_cannot_be_used_to_redirect_a_write(tmp_path, monkeypatch, capsys, _seat_home):
    """The once-marker lives in the seat's probe stamp; a link planted AT the stamp
    must not be written through either."""
    tbh = _load_time_bar()
    stamp = _seat_home / "probe-unknown-notice.json"
    protected = tmp_path / "protected-stamp.bin"
    protected.write_bytes(PAYLOAD)
    os.symlink(protected, stamp)
    activity = tmp_path / "last-user-activity"
    target = tmp_path / "protected.bin"
    target.write_bytes(PAYLOAD)
    os.symlink(target, activity)
    monkeypatch.setattr(tbh, "LAST_USER_ACTIVITY", activity)
    _run_main(tbh, tmp_path, monkeypatch, capsys)
    assert target.read_bytes() == PAYLOAD
    assert protected.read_bytes() == PAYLOAD


# ------------------------------------------------------------------ (4) the direct writers

def _load_tool(name):
    for p in (str(TOOLS_DIR), str(SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(f"{name}_nt4380", TOOLS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
@pytest.mark.parametrize("fn_name", ["_write_sheet", "_write_key"])
def test_quote_quality_survey_writers_refuse_links(tmp_path, how, fn_name):
    mod = _load_tool("quote_quality_survey")
    target, victim = _plant(tmp_path, how)
    fn = getattr(mod, fn_name)
    with pytest.raises(OSError):
        fn(victim, "content")
    assert target.read_bytes() == PAYLOAD


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
def test_s2_clause_sheet_rewrite_private_refuses_links(tmp_path, how):
    mod = _load_tool("s2_clause_sheet")
    target, victim = _plant(tmp_path, how)
    with pytest.raises(OSError):
        mod._rewrite_private(victim, "content")
    assert target.read_bytes() == PAYLOAD


# ------------------------------------------------ (5) engram_core backup-push status tmp

@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
def test_backup_push_status_tmp_plant_leaves_target_unchanged(tmp_path, monkeypatch, how):
    for p in (str(SRC),):
        if p not in sys.path:
            sys.path.insert(0, p)
    import engram_core
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(engram_core, "DATA_DIR", data)
    target = tmp_path / "protected.bin"
    target.write_bytes(PAYLOAD)
    tmp_name = data / ".backup-push-status.json.tmp"
    if how == "symlink":
        os.symlink(target, tmp_name)
    else:
        os.link(target, tmp_name)
    engram_core._write_backup_push_status(
        result="ok", stderr="", commit_sha="abc", ahead=0, behind=0, rewrite_guard={})
    assert target.read_bytes() == PAYLOAD
    status = json.loads((data / "backup-push-status.json").read_text())
    assert status["result"] == "ok"
    assert not tmp_name.exists()  # renamed into place, nothing left behind


# ------------------------------------------------ (6) the surface hook's tmp site (routing)

def test_surface_hook_tmp_open_goes_through_the_strict_helper():
    """The `PRINCIPLE_TRIGGER_STATE_PATH + ".tmp"` open in engram-surface-hook.py sits in
    the branch taken only when `_hooklib.atomic_write_json` failed to import, so no cell
    can drive it. Pin its ROUTING instead: the open is the `_hooklib` helper
    `open_regular_text_write` (imported by name from `_hooklib`), which this change made
    strict, and not a raw open of the deterministic tmp name."""
    import ast
    tree = ast.parse((HOOK_DIR / "engram-surface-hook.py").read_text(encoding="utf-8"))
    imported_from_hooklib = {
        a.asname or a.name
        for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "_hooklib"
        for a in n.names
    }
    assert "open_regular_text_write" in imported_from_hooklib
    tmp_opens = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "open_regular_text_write"
        and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "tmp"
    ]
    assert len(tmp_opens) == 1, "expected exactly one tmp open through the helper"


# ------------------------------------------------ (7) round 2 (PR review of b2f78458)

LOOP_ARM_HOOK = HOOK_DIR / "engram-loop-arm-stop-hook.py"
SURFACE_HOOK = HOOK_DIR / "engram-surface-hook.py"
LOG_LIMIT = 64 * 1024  # _REPLY_GATE_LOG_MAX_BYTES in the hook


def _load_hook(name, path):
    for p in (str(HOOK_DIR), str(SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plant_sized(tmp_path, how, size):
    """Like _plant, with a target of `size` bytes (a rotation needs > 64 KiB)."""
    target = tmp_path / "target.bin"
    target.write_bytes(b"P" * size)
    victim = tmp_path / "victim.log"
    if how == "symlink":
        os.symlink(target, victim)
    else:
        os.link(target, victim)
    return target, victim


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
@pytest.mark.parametrize("size,path_name", [(4096, "append"), (LOG_LIMIT + 4096, "rotation")])
def test_loop_arm_reply_gate_log_never_writes_through_a_link(tmp_path, capsys, how, size, path_name):
    """B1: `_log_reply_gate` appended into (small log) or truncated (log over 64 KiB) a
    link's target. Both the append path and the rotation path must leave the target
    byte-identical, raise nothing, and say so once on stderr."""
    mod = _load_hook("loop_arm_hook_4380", LOOP_ARM_HOOK)
    target, victim = _plant_sized(tmp_path, how, size)
    before = target.read_bytes()
    mod._REPLY_GATE_LOG = str(victim)
    assert mod._log_reply_gate("sid", 5, (True, False), "block") is None  # fail-silent
    assert target.read_bytes() == before
    err = capsys.readouterr().err
    assert str(victim) in err and "regular file" in err


@_NEEDS_LINKS
@pytest.mark.parametrize("size", [4096, LOG_LIMIT + 4096])
def test_loop_arm_reply_gate_log_regular_file_still_logs_and_rotates(tmp_path, size):
    """Positive control for the cell above: a regular log is still appended to (small)
    or trimmed to the kept tail plus the new line (over the limit)."""
    mod = _load_hook("loop_arm_hook_4380b", LOOP_ARM_HOOK)
    log = tmp_path / "gate.log"
    log.write_bytes(b"old line\n" * (size // 9))
    mod._REPLY_GATE_LOG = str(log)
    mod._log_reply_gate("sid", 5, (True, False), "block")
    lines = log.read_text().splitlines()
    assert lines[-1].endswith("decision=block")
    if size > LOG_LIMIT:
        assert len(lines) == mod._REPLY_GATE_LOG_KEEP_LINES + 1
    else:
        assert len(lines) == size // 9 + 1


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
def test_loop_arm_gate_decision_is_unchanged_with_a_link_at_the_log(tmp_path, how):
    """B1 end to end through the real hook: the gate still blocks a swallowed reply, and
    the planted link's target is untouched."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_loop_arm_reply_gate_payload_4193 import SWALLOWED, run, kind, LOG_NAME
    target = tmp_path / "protected.bin"
    target.write_bytes(PAYLOAD)

    def prepare(home):
        (os.symlink if how == "symlink" else os.link)(target, home / LOG_NAME)

    out, _home = run(tmp_path / "run", SWALLOWED, "the reply", prepare=prepare)
    assert kind(out) == "reply"
    assert target.read_bytes() == PAYLOAD


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
def test_backup_remote_repair_status_tmp_plant_leaves_target_unchanged(tmp_path, how):
    """S1: the same deterministic-tmp write as engram_core's backup-push status."""
    mod = _load_tool("backup_remote_repair")
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "protected.bin"
    target.write_bytes(PAYLOAD)
    tmp_name = home / ("." + mod.STATUS_NAME + ".tmp")
    (os.symlink if how == "symlink" else os.link)(target, tmp_name)
    mod._write_status(str(home), "ok", "", "abc", (0, 0))
    assert target.read_bytes() == PAYLOAD
    assert json.loads((home / mod.STATUS_NAME).read_text())["result"] == "ok"
    assert not tmp_name.exists()


@_NEEDS_LINKS
@pytest.mark.parametrize("how", ["symlink", "hardlink"])
@pytest.mark.parametrize("which", ["counter", "write_reminder", "repair_marker"])
def test_surface_hook_no_hooklib_fallbacks_survive_a_refused_link(tmp_path, monkeypatch, how, which):
    """S3: with `_HOOKLIB_OK` false the three fallback branches use the strict helper,
    which now raises NotRegularFile for a link. None may let it escape, and the link's
    target must be unchanged."""
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "seat"))
    mod = _load_hook("surface_hook_4380", SURFACE_HOOK)
    mod._HOOKLIB_OK = False
    target = tmp_path / "protected.json"
    body = b'{"pending": true, "prompts_since_compaction": 1, "last_reset": ""}'
    target.write_bytes(body)
    victim = tmp_path / "victim.json"
    (os.symlink if how == "symlink" else os.link)(target, victim)
    if which == "counter":
        mod.COUNTER_PATH = str(victim)
        mod.write_prompt_counter({"prompts_since_compaction": 2, "last_reset": "x"})
    elif which == "write_reminder":
        mod.WRITE_REMINDER_PATH = str(victim)
        mod.check_write_reminder()
    else:
        mod.REPAIR_MARKER_PATH = str(victim)
        mod.check_repair_marker()
    assert target.read_bytes() == body
