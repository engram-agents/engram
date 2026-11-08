"""Contract cells for .github/ci/nightly_rss_plugin.py (#4420, per-test memory attribution).

Contract (stated before the plugin existed):

C7a  a test whose process RSS rose >= 20 MB over the previous reading makes the
     plugin print one ``[rss-delta] <nodeid> +<MB>`` line; a smaller rise prints none.
C7b  at session end the plugin writes the top 30 tests by delta and the top 20 files
     by summed delta to the file named by ``NIGHTLY_RSS_OUT``.
C7c  every hook is guarded: an unreadable ``/proc/self/status`` (or any internal
     failure) never raises and never fails a test.
C7d  with no ``/proc`` (macOS) the plugin is a silent no-op.
"""

import importlib.util
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CI_DIR = REPO / ".github" / "ci"
PLUGIN = CI_DIR / "nightly_rss_plugin.py"

MB = 1024  # the plugin reads VmRSS in kB


@pytest.fixture()
def plug():
    spec = importlib.util.spec_from_file_location("nightly_rss_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._reset()
    return mod


def _feed(mod, monkeypatch, steps):
    """steps: list of (nodeid, rss_kb_at_start, rss_kb_at_finish)."""
    readings = []
    for _, a, b in steps:
        readings += [a, b]
    it = iter(readings)
    monkeypatch.setattr(mod, "_read_rss_kb", lambda: next(it))
    for nodeid, _, _ in steps:
        mod.pytest_runtest_logstart(nodeid, (nodeid.split("::")[0], 0, nodeid))
        mod.pytest_runtest_logfinish(nodeid, (nodeid.split("::")[0], 0, nodeid))


def test_c7a_a_50mb_rise_emits_a_line_and_a_5mb_rise_emits_none(plug, monkeypatch, capsys):
    _feed(plug, monkeypatch, [
        ("tests/test_a.py::test_small", 100 * MB, 105 * MB),
        ("tests/test_a.py::test_big", 105 * MB, 155 * MB),
    ])
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if "[rss-delta]" in ln]
    assert len(lines) == 1, out
    assert re.search(r"\[rss-delta\] tests/test_a\.py::test_big \+50\b", lines[0]), lines[0]
    assert "test_small" not in out


def test_c7a_threshold_is_twenty_mb_over_the_previous_reading(plug, monkeypatch, capsys):
    _feed(plug, monkeypatch, [
        ("tests/test_a.py::t19", 100 * MB, 119 * MB),
        ("tests/test_a.py::t20", 119 * MB, 139 * MB),
    ])
    out = capsys.readouterr().out
    assert "t19" not in out
    assert "t20" in out


def test_c7b_session_end_writes_top_tests_and_top_files(plug, monkeypatch, tmp_path):
    out_path = tmp_path / "rss.md"
    monkeypatch.setenv("NIGHTLY_RSS_OUT", str(out_path))
    steps = []
    cur = 100 * MB
    for i in range(40):
        grow = (i + 1) * MB
        steps.append((f"tests/test_f{i % 25:02d}.py::test_{i:02d}", cur, cur + grow))
        cur += grow
    _feed(plug, monkeypatch, steps)
    plug.pytest_sessionfinish(session=None, exitstatus=0)
    text = out_path.read_text(encoding="utf-8")
    test_rows = re.findall(r"^\| `(tests/[^`]+::[^`]+)` \| (\d+(?:\.\d+)?) \|$", text, flags=re.M)
    file_rows = re.findall(r"^\| `(tests/test_f\d+\.py)` \| (\d+(?:\.\d+)?) \|$", text, flags=re.M)
    assert len(test_rows) == 30
    assert len(file_rows) == 20
    vals = [float(v) for _, v in test_rows]
    assert vals == sorted(vals, reverse=True)
    fvals = [float(v) for _, v in file_rows]
    assert fvals == sorted(fvals, reverse=True)
    # The biggest single test is the last one fed (40 MB).
    assert test_rows[0][0] == "tests/test_f14.py::test_39"
    # Files are summed: f14 holds tests 14 and 39 (15 + 40 MB).
    assert dict(file_rows)["tests/test_f14.py"] in ("55", "55.0")


def test_c7b_no_output_file_when_the_env_is_unset(plug, monkeypatch, tmp_path):
    monkeypatch.delenv("NIGHTLY_RSS_OUT", raising=False)
    _feed(plug, monkeypatch, [("tests/test_a.py::t", 1 * MB, 90 * MB)])
    plug.pytest_sessionfinish(session=None, exitstatus=0)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("exc", [OSError("no /proc"), PermissionError("denied"), ValueError("bad")])
def test_c7c_unreadable_proc_status_never_raises(plug, monkeypatch, capsys, tmp_path, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(plug, "open", boom, raising=False)
    monkeypatch.setenv("NIGHTLY_RSS_OUT", str(tmp_path / "rss.md"))
    plug.pytest_runtest_logstart("tests/test_a.py::t", ("tests/test_a.py", 0, "t"))
    plug.pytest_runtest_logfinish("tests/test_a.py::t", ("tests/test_a.py", 0, "t"))
    plug.pytest_sessionfinish(session=None, exitstatus=0)
    assert "[rss-delta]" not in capsys.readouterr().out


@pytest.mark.parametrize("exc", [OSError("no /proc"), PermissionError("denied"), ValueError("bad")])
def test_c7c_the_reader_itself_returns_none_when_proc_is_unreadable(plug, monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(plug, "open", boom, raising=False)
    assert plug._read_rss_kb() is None


def test_c7c_garbage_status_content_never_raises(plug, monkeypatch, tmp_path):
    p = tmp_path / "status"
    p.write_text("VmRSS:\tnot-a-number kB\n", encoding="utf-8")
    monkeypatch.setattr(plug, "STATUS_PATH", str(p))
    plug.pytest_runtest_logstart("tests/test_a.py::t", ("tests/test_a.py", 0, "t"))
    plug.pytest_runtest_logfinish("tests/test_a.py::t", ("tests/test_a.py", 0, "t"))


def test_c7c_a_failing_internal_never_raises(plug, monkeypatch):
    monkeypatch.setattr(plug, "_read_rss_kb", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    plug.pytest_runtest_logstart(None, None)
    plug.pytest_runtest_logfinish(None, None)
    plug.pytest_sessionfinish(session=None, exitstatus=0)


def _run_pytest_with_plugin(tmp_path, body, env_extra=None):
    (tmp_path / "test_gen.py").write_text(textwrap.dedent(body), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(CI_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "nightly_rss_plugin", "-p", "no:cacheprovider",
         "-v", "test_gen.py"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )


_ALLOC = '''
HOLD = []

def test_holds_50mb():
    buf = bytearray(50 * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    HOLD.append(buf)

def test_holds_5mb():
    buf = bytearray(5 * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    HOLD.append(buf)
'''


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs /proc (Linux)")
def test_c7a_real_process_a_held_50mb_allocation_is_reported_and_5mb_is_not(tmp_path):
    out = tmp_path / "rss.md"
    r = _run_pytest_with_plugin(tmp_path, _ALLOC, {"NIGHTLY_RSS_OUT": str(out)})
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("[rss-delta]")]
    assert any("test_holds_50mb" in ln for ln in lines), r.stdout
    assert not any("test_holds_5mb" in ln for ln in lines), r.stdout
    assert out.exists() and "test_holds_50mb" in out.read_text(encoding="utf-8")


def test_c7d_no_proc_is_a_silent_noop(tmp_path):
    # NIGHTLY_RSS_STATUS_PATH points at a file that does not exist: the same
    # condition as a host with no /proc (macOS), reproduced on any platform.
    out = tmp_path / "rss.md"
    r = _run_pytest_with_plugin(tmp_path, _ALLOC, {
        "NIGHTLY_RSS_STATUS_PATH": str(tmp_path / "does-not-exist"),
        "NIGHTLY_RSS_OUT": str(out),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[rss-delta]" not in r.stdout
    assert "Traceback" not in r.stdout + r.stderr
    assert not out.exists()
