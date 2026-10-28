"""#3521 row 6: a config.json that cannot be read is UNKNOWN, never "not configured".

`_prompthooklib.load_config` returned `{}` for ANY failure, so a corrupt file
read exactly like an absent one and the forum, baton and inter-agent prompt
hooks all exited silently. Contract:

  C1  load_config_checked(): absent -> ({}, None); valid object -> (dict, None);
      OSError -> (None, "unreadable: <Class>"); not JSON -> (None, "unparsable:
      <Class>"); JSON but not an object -> (None, "not-an-object: <json type>").
  C2  load_config() keeps returning {} on failure.
  C3  each hook, on (None, reason): one stderr line, ONE rate-limited injected
      line (stamp written AFTER the emit), exit 0.
  C4  absent / valid configs: byte-identical to before (silent when unconfigured).
  C5  the same holds in the FLATTENED deployed layout, CLAUDE_PLUGIN_ROOT unset.
  C6  every scenario exits 0.

The hooks run as real subprocesses (nothing is monkeypatched), against a tmp
ENGRAM_HOME.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"
PROMPTHOOKLIB = HOOKS_DIR / "_prompthooklib.py"

HOOKS = {
    "forum": "engram-forum-prompt-hook.py",
    "baton": "engram-baton-prompt-hook.py",
    "inter-agent": "engram-inter-agent-prompt-hook.py",
}
STAMP_KEY = {
    "forum": "config:forum-prompt-hook",
    "baton": "config:baton-prompt-hook",
    "inter-agent": "config:inter-agent-prompt-hook",
}
STAMP_NAME = "probe-unknown-notice.json"
SIBLINGS = ["_hooklib.py", "_prompthooklib.py", "_harness.py"]

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_lib():
    spec = importlib.util.spec_from_file_location("_prompthooklib_under_test", PROMPTHOOKLIB)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(HOOKS_DIR))
    return mod


lib = _load_lib()


# ---------------------------------------------------------------------------
# C1 / C2 unit cells
# ---------------------------------------------------------------------------

def _cfg(tmp_path, data: bytes):
    (tmp_path / "config.json").write_bytes(data)
    return lib.load_config_checked(str(tmp_path))


def test_checked_absent_is_empty_dict_no_reason(tmp_path):
    assert lib.load_config_checked(str(tmp_path)) == ({}, None)


def test_checked_absent_home_itself_is_absent(tmp_path):
    assert lib.load_config_checked(str(tmp_path / "nope")) == ({}, None)


def test_checked_valid_object(tmp_path):
    assert _cfg(tmp_path, b'{"agent_name": "x"}') == ({"agent_name": "x"}, None)


def test_checked_valid_empty_object_is_a_dict_not_unknown(tmp_path):
    assert _cfg(tmp_path, b"{}") == ({}, None)


def test_checked_default_home_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"a": 1}')
    assert lib.load_config_checked() == ({"a": 1}, None)


@pytest.mark.parametrize("data,cls", [
    (b"", "JSONDecodeError"),
    (b"{not json", "JSONDecodeError"),
    (b'{"a": ', "JSONDecodeError"),
    (b"\x00\x00\x00", "JSONDecodeError"),
    (b"\xff\xfe{}", "UnicodeDecodeError"),
    pytest.param(b"[" * 200000, "RecursionError", id="deep-nesting"),
])
def test_checked_unparsable(tmp_path, data, cls):
    assert _cfg(tmp_path, data) == (None, f"unparsable: {cls}")


@pytest.mark.parametrize("data,name", [
    (b"[1, 2]", "array"),
    (b'"x"', "string"),
    (b"42", "number"),
    (b"1.5", "number"),
    (b"null", "null"),
    (b"true", "boolean"),
])
def test_checked_not_an_object(tmp_path, data, name):
    assert _cfg(tmp_path, data) == (None, f"not-an-object: {name}")


def test_checked_directory_in_place_is_unreadable(tmp_path):
    (tmp_path / "config.json").mkdir()
    cfg, reason = lib.load_config_checked(str(tmp_path))
    assert cfg is None and reason == "unreadable: IsADirectoryError"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_checked_mode_000_is_unreadable(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}")
    p.chmod(0)
    try:
        assert lib.load_config_checked(str(tmp_path)) == (None, "unreadable: PermissionError")
    finally:
        p.chmod(0o600)


def test_checked_reason_follows_probe_convention(tmp_path):
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        import _hooklib
        _, reason = _cfg(tmp_path, b"[]")
        cls, msg = _hooklib.split_probe_reason(reason)
        assert (cls, msg) == ("not-an-object", "array")
    finally:
        sys.path.remove(str(HOOKS_DIR))


def test_checked_never_raises_on_unusable_home(tmp_path):
    # ENGRAM_HOME that is a file, and a NUL in the path: both must return, not raise.
    f = tmp_path / "afile"
    f.write_text("x")
    for home in (str(f), str(tmp_path) + "/a\x00b"):
        cfg, reason = lib.load_config_checked(home)
        assert (cfg, reason) == ({}, None) or (cfg is None and reason)


@pytest.mark.parametrize("data", [b"", b"{x", b"[1]", b"null", b"\x00"])
def test_load_config_still_returns_empty_dict_on_failure(tmp_path, data):
    (tmp_path / "config.json").write_bytes(data)
    assert lib.load_config(str(tmp_path)) == {}


def test_load_config_still_returns_dict_on_success(tmp_path):
    (tmp_path / "config.json").write_text('{"k": "v"}')
    assert lib.load_config(str(tmp_path)) == {"k": "v"}


# ---------------------------------------------------------------------------
# C3 / C4 / C6 hook cells (real subprocesses)
# ---------------------------------------------------------------------------

def _run(hook, home, layout=None, timeout=60):
    """Run the real hook; layout=None -> the source tree, else a flattened
    deployed copy's hooks/ dir. CLAUDE_PLUGIN_ROOT is always unset."""
    base = layout if layout is not None else HOOKS_DIR
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "FORUM_URL", "ENGRAM_DATA_DIR")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(base / HOOKS[hook])],
        input="{}", capture_output=True, text=True, env=env, timeout=timeout,
    )


def _flattened(tmp_path):
    root = tmp_path / "deployed"
    (root / "hooks").mkdir(parents=True)
    (root / "tools").mkdir()
    for name in list(HOOKS.values()) + SIBLINGS:
        shutil.copy(HOOKS_DIR / name, root / "hooks" / name)
    (root / "tools" / "_status_derive.py").write_text(
        "def derive_own_status(agent_name): return None, None, None, None\n"
        "def _read_loop_mode(): return None\n"
    )
    (root / "tools" / "forum_api.py").write_text(
        (HOOKS_DIR.parent.parent / "tools" / "forum_api.py").read_text()
        if (HOOKS_DIR.parent.parent / "tools" / "forum_api.py").exists() else "# marker\n"
    )
    return root / "hooks"


def _home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def _stamp(home):
    p = home / STAMP_NAME
    return json.loads(p.read_text())["shown_at"] if p.exists() else None


def _injected(r):
    """The additionalContext of the single JSON envelope on stdout, or ''."""
    out = r.stdout.strip()
    if not out:
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture(params=list(HOOKS))
def hook(request):
    return request.param


def test_absent_config_is_silent(hook, tmp_path):
    home = _home(tmp_path)
    r = _run(hook, home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _stamp(home) is None


def test_valid_unconfigured_config_is_silent(hook, tmp_path):
    home = _home(tmp_path)
    (home / "config.json").write_text("{}")
    r = _run(hook, home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _stamp(home) is None


def test_valid_configured_config_behaves_as_before(hook, tmp_path):
    """Control: a configured forum on a closed port. Whatever the hook does
    about an unreachable forum, it must NOT claim the config was unreadable."""
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps(
        {"agent_name": "tester", "forum": {"url": "http://127.0.0.1:1"}}))
    r = _run(hook, home)
    assert r.returncode == 0
    assert "config.json" not in r.stdout and "config.json" not in r.stderr
    assert "UNKNOWN" not in _injected(r) or "config" not in _injected(r)
    assert _stamp(home) is None or STAMP_KEY[hook] not in _stamp(home)


def test_corrupt_config_reports_unknown_once_per_window(hook, tmp_path):
    home = _home(tmp_path)
    (home / "config.json").write_text("{not json")

    r1 = _run(hook, home)
    assert r1.returncode == 0
    err_lines = [l for l in r1.stderr.splitlines() if l.strip()]
    assert len(err_lines) == 1, r1.stderr
    assert "config unknown: unparsable: JSONDecodeError" in err_lines[0]
    # The forum hook's shared renderer prepends its own "shown/cut" trailer
    # line; the injected probe line itself is exactly one bounded line.
    injected = _injected(r1)
    probe_lines = [l for l in injected.splitlines() if l.startswith("[probe]")]
    assert len(probe_lines) == 1 and len(probe_lines[0]) <= 200
    line = probe_lines[0]
    assert "config.json" in line and "UNKNOWN" in line and "unparsable" in line
    stamp = _stamp(home)
    assert stamp is not None and STAMP_KEY[hook] in stamp
    assert stamp[STAMP_KEY[hook]] <= time.time()

    r2 = _run(hook, home)
    assert r2.returncode == 0
    assert [l for l in r2.stderr.splitlines() if l.strip()] == err_lines
    assert r2.stdout == ""


def test_stamp_keys_are_distinct_per_hook(tmp_path):
    home = _home(tmp_path)
    (home / "config.json").write_text("{not json")
    for h in HOOKS:
        assert _injected(_run(h, home)) != "", f"{h} swallowed by another hook's stamp"
    assert set(STAMP_KEY.values()) <= set(_stamp(home))


def _make_unreadable(kind, home):
    p = home / "config.json"
    if kind == "empty":
        p.write_bytes(b"")
    elif kind == "nul":
        p.write_bytes(b"\x00\x00")
    elif kind == "list":
        p.write_text("[1,2]")
    elif kind == "string":
        p.write_text('"x"')
    elif kind == "null":
        p.write_text("null")
    elif kind == "number":
        p.write_text("42")
    elif kind == "dir":
        p.mkdir()
    elif kind == "mode000":
        if os.geteuid() == 0:
            pytest.skip("root ignores file modes")
        p.write_text("{}")
        p.chmod(0)
    return p


@pytest.mark.parametrize("kind", [
    "empty", "nul", "list", "string", "null", "number", "dir", "mode000"])
def test_unreadable_shapes_are_unknown_not_silent(hook, tmp_path, kind):
    home = _home(tmp_path)
    p = _make_unreadable(kind, home)
    try:
        r = _run(hook, home)
    finally:
        if kind == "mode000":
            p.chmod(0o600)
    assert r.returncode == 0
    assert "config unknown:" in r.stderr
    line = _injected(r)
    assert "config.json" in line and "UNKNOWN" in line
    assert STAMP_KEY[hook] in (_stamp(home) or {})


@pytest.mark.parametrize("stamp", ["garbage", "future", "wrong-shape", "string-ts"])
def test_bad_stamp_fails_visible(hook, tmp_path, stamp):
    home = _home(tmp_path)
    (home / "config.json").write_text("{not json")
    path = home / STAMP_NAME
    if stamp == "garbage":
        path.write_bytes(b"\x00not json")
    elif stamp == "future":
        path.write_text(json.dumps({"shown_at": {STAMP_KEY[hook]: time.time() + 99999}}))
    elif stamp == "wrong-shape":
        path.write_text(json.dumps(["x"]))
    else:
        path.write_text(json.dumps({"shown_at": {STAMP_KEY[hook]: "yesterday"}}))
    r = _run(hook, home)
    assert r.returncode == 0
    assert "config.json" in _injected(r)


def test_unwritable_stamp_does_not_crash(hook, tmp_path):
    home = _home(tmp_path)
    (home / "config.json").write_text("{not json")
    (home / STAMP_NAME).mkdir()  # a directory where the stamp file goes
    r = _run(hook, home)
    assert r.returncode == 0
    assert "config.json" in _injected(r)


# ---------------------------------------------------------------------------
# C5 flattened deployed layout, CLAUDE_PLUGIN_ROOT unset
# ---------------------------------------------------------------------------

def test_flattened_layout_absent_silent_corrupt_unknown(hook, tmp_path):
    layout = _flattened(tmp_path)
    home = _home(tmp_path)
    r = _run(hook, home, layout)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    (home / "config.json").write_text("{not json")
    r = _run(hook, home, layout)
    assert r.returncode == 0
    assert "config unknown:" in r.stderr
    assert "config.json" in _injected(r)
    assert STAMP_KEY[hook] in (_stamp(home) or {})
    r = _run(hook, home, layout)
    assert r.stdout == "" and "config unknown:" in r.stderr


def test_flattened_layout_without_hooklib_still_not_silent(hook, tmp_path):
    """_hooklib missing: stderr line only (no stamp machinery), exit 0."""
    layout = _flattened(tmp_path)
    if hook == "forum":
        pytest.skip("the forum hook imports _hooklib unconditionally at module load")
    (layout / "_hooklib.py").unlink()
    home = _home(tmp_path)
    (home / "config.json").write_text("{not json")
    r = _run(hook, home, layout)
    assert r.returncode == 0
    assert "config unknown:" in r.stderr


# ---------------------------------------------------------------------------
# The stamp is written strictly AFTER a successful flush (in-process)
# ---------------------------------------------------------------------------

def _load_hook_module(name, monkeypatch, home):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location(
        f"_cfgunknown_{name.replace('-', '_')}", HOOKS_DIR / HOOKS[name])
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(HOOKS_DIR))
    return mod


def test_stamp_not_written_when_the_flush_fails(hook, tmp_path, monkeypatch):
    """print() succeeds, only flush() raises: the line was buffered but never
    delivered, so it must NOT start a rate-limit window. The stderr line is
    unaffected."""
    import io
    home = _home(tmp_path)
    (home / "config.json").write_text("{not json")
    mod = _load_hook_module(hook, monkeypatch, home)

    class _FlushFails(io.StringIO):
        def flush(self):
            raise OSError("flush failed: broken pipe")

    out, err = _FlushFails(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code in (0, None)
    assert "config.json" in out.getvalue(), "the print itself must have succeeded"
    assert "config unknown:" in err.getvalue()
    assert not (home / STAMP_NAME).exists(), "an unflushed line must leave no stamp"


def test_stamp_written_when_the_flush_succeeds_in_process(hook, tmp_path, monkeypatch):
    """Control for the cell above: same in-process run, working stdout."""
    import io
    home = _home(tmp_path)
    (home / "config.json").write_text("{not json")
    mod = _load_hook_module(hook, monkeypatch, home)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit):
        mod.main()
    assert "config.json" in out.getvalue()
    assert STAMP_KEY[hook] in (_stamp(home) or {})
