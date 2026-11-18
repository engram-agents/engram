"""Tests for tools/run_matrix.py — the #864 multi-env harness driver.

Docker is fully mocked (a fake `runner` standing in for subprocess.run), so
these are deterministic and run anywhere — no docker, no images, no network.
The driver's contract under test:
  - selection is composed from run_touched_tests.py --json (parsed, never scraped)
  - a stale/missing image is EXCLUDED loudly (exit 2), never silently skipped
  - exit status aggregates per-env pytest return codes (no pipe between check/gate)
  - docker-absent (non-dry-run) is a usage error (exit 3), not a false pass
"""
import json
import sys
from pathlib import Path

# append (not insert(0)) -- src/engram/tools/ also holds forum.py (the CLI
# script tools/forum.py symlinks to); making it a sys.path *root* at
# position 0 would outrank src/forum for the rest of the pytest run,
# shadowing the real `forum` package for every test collected afterward
# (#2037/#2063).
sys.path.append(str(Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"))
import run_matrix as rm  # noqa: E402


# ---------------------------------------------------------------------------
# Fake subprocess runner
# ---------------------------------------------------------------------------
class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(*, selection=None, selection_rc=0, selection_stdout=None,
                stamp="STALE-DEFAULT", build_rc=0, pytest_rc=0, pytest_out="1 passed"):
    """Return a fake runner that dispatches on the command shape.

    `stamp` is the label string `docker image inspect` returns: pass `None` to
    simulate an absent image, or any string to simulate that baked stamp. The
    default never equals a real computed hash, so the image reads as stale —
    use `fresh_runner()` (below) when you need the image to read as fresh.
    """
    sel = selection if selection is not None else {
        "base": "origin/dev", "changed_files": ["engram_stats.py"], "full": False,
        "full_reasons": [], "selected": ["tests/test_a.py", "tests/test_b.py"],
        "total_test_files": 100,
        "buckets": {"internals": 2, "contract": 0, "map_residue": 0},
    }
    sel_out = selection_stdout if selection_stdout is not None else json.dumps(sel)

    def runner(cmd, capture_output=False, text=False, **kw):
        # selection emitter
        if any("run_touched_tests.py" in c for c in cmd):
            return FakeProc(selection_rc, sel_out, "" if selection_rc == 0 else "emit boom")
        # docker image inspect (stamp lookup)
        if cmd[:3] == ["docker", "image", "inspect"]:
            if stamp is None:
                return FakeProc(1, "", "No such image")
            return FakeProc(0, stamp + "\n")
        # docker build
        if cmd[:2] == ["docker", "build"]:
            return FakeProc(build_rc)
        # docker run pytest
        if cmd[:2] == ["docker", "run"]:
            return FakeProc(pytest_rc, pytest_out, "")
        raise AssertionError(f"unexpected command: {cmd}")

    return runner, sel


# A runner whose stamp always equals the freshly computed hash (always-fresh).
def fresh_runner(**kw):
    base_runner, sel = make_runner(**kw)

    def runner(cmd, capture_output=False, text=False, **kwargs):
        if cmd[:3] == ["docker", "image", "inspect"]:
            # echo the hash the driver would compute for the env in the tag
            tag = cmd[-1]
            name = tag.split(":", 1)[1]
            env = rm._envs_by_name()[name]
            h = rm.compute_image_hash(env, rm.REPO_ROOT_DEFAULT)
            return FakeProc(0, h + "\n")
        return base_runner(cmd, capture_output=capture_output, text=text, **kwargs)

    return runner, sel


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_compute_selection_parses_json():
    runner, sel = make_runner()
    out = rm.compute_selection("origin/dev", False, rm.REPO_ROOT_DEFAULT, runner=runner)
    assert out["selected"] == ["tests/test_a.py", "tests/test_b.py"]
    assert out["buckets"]["internals"] == 2


def test_compute_selection_raises_on_nonzero():
    runner, _ = make_runner(selection_rc=1)
    try:
        rm.compute_selection("origin/dev", False, rm.REPO_ROOT_DEFAULT, runner=runner)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "emitter exited 1" in str(e)


def test_compute_selection_raises_on_bad_json():
    runner, _ = make_runner(selection_stdout="not json at all")
    try:
        rm.compute_selection("origin/dev", False, rm.REPO_ROOT_DEFAULT, runner=runner)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "valid JSON" in str(e)


# ---------------------------------------------------------------------------
# Full-suite trigger stop (#4358 / PR-MERGE-POLICY merge-seat test rules item 1)
# ---------------------------------------------------------------------------
TRIGGER_SEL = {
    "base": "origin/dev", "changed_files": ["pytest.ini", "tests/conftest.py"],
    "full": True,
    "full_reasons": ["pytest.ini changed", "tests/conftest.py changed"],
    "selected": [], "total_test_files": 100,
    "buckets": {"internals": 0, "contract": 0, "map_residue": 0},
}


def test_compute_selection_trigger_exit7_raises_named_stop():
    # M1: exit 7 + a full:true JSON object -> FullTriggerStop naming every
    # triggering file and pointing at --full; never a returned selection.
    runner, _ = make_runner(selection=TRIGGER_SEL, selection_rc=7)
    try:
        rm.compute_selection("origin/dev", False, rm.REPO_ROOT_DEFAULT, runner=runner)
        assert False, "expected FullTriggerStop"
    except rm.FullTriggerStop as e:
        msg = str(e)
        assert isinstance(e, RuntimeError)
        assert "pytest.ini changed" in msg and "tests/conftest.py changed" in msg
        assert "--full" in msg
        assert "emit boom" not in msg


def test_run_matrix_trigger_exit7_runs_no_environment(monkeypatch):
    # M1: run_matrix stops; no docker command of any kind is issued.
    monkeypatch.setattr(rm, "docker_available", lambda: True)
    inner, _ = make_runner(selection=TRIGGER_SEL, selection_rc=7)
    docker_calls = []

    def runner(cmd, **kw):
        if cmd and cmd[0] == "docker":
            docker_calls.append(cmd)
        return inner(cmd, **kw)

    try:
        rm.run_matrix(Args(), runner=runner)
        assert False, "expected FullTriggerStop"
    except rm.FullTriggerStop:
        pass
    assert docker_calls == []


def test_main_trigger_stop_names_files_and_exits_3(monkeypatch, capsys):
    def stop(*a, **k):
        raise rm.FullTriggerStop(["pytest.ini changed"])
    monkeypatch.setattr(rm, "compute_selection", stop)
    monkeypatch.setattr(rm, "docker_available", lambda: True)
    assert rm.main(["--envs", "p0"]) == 3
    err = capsys.readouterr().err
    assert "pytest.ini changed" in err and "--full" in err


def test_compute_selection_exit7_without_json_object_is_plain_error():
    # M2: exit 7 whose stdout is not a full:true object is still an error
    # naming exit 7 (no silent success), and not the trigger stop.
    for bad in ("", "not json", json.dumps([1]), json.dumps({"full": False}),
                json.dumps({"selected": ["tests/a.py"]})):
        runner, _ = make_runner(selection_rc=7, selection_stdout=bad)
        try:
            rm.compute_selection("origin/dev", False, rm.REPO_ROOT_DEFAULT, runner=runner)
            assert False, f"expected RuntimeError for stdout {bad!r}"
        except rm.FullTriggerStop:
            assert False, f"must not be the trigger stop for stdout {bad!r}"
        except RuntimeError as e:
            assert "exited 7" in str(e)


def test_compute_selection_other_nonzero_unchanged():
    # M3: any other non-zero exit keeps the old message, even with a trigger-
    # shaped body on stdout.
    for rc in (1, 2, 4, 6):
        runner, _ = make_runner(selection=TRIGGER_SEL, selection_rc=rc)
        try:
            rm.compute_selection("origin/dev", False, rm.REPO_ROOT_DEFAULT, runner=runner)
            assert False, "expected RuntimeError"
        except rm.FullTriggerStop:
            assert False, f"exit {rc} must not be the trigger stop"
        except RuntimeError as e:
            assert f"emitter exited {rc}: emit boom" in str(e)


def test_run_matrix_full_flag_passes_full_and_never_stops(monkeypatch):
    # M4: --full is forwarded to the emitter, which then exits 0 with the
    # full object; the stop is never reached. A fake emitter that would exit 7
    # without --full proves the flag is what avoids the stop.
    monkeypatch.setattr(rm, "docker_available", lambda: False)
    full_sel = dict(TRIGGER_SEL, full_reasons=["--full flag"], changed_files=[])
    seen = []

    def runner(cmd, **kw):
        assert any("run_touched_tests.py" in c for c in cmd)
        seen.append(cmd)
        if "--full" in cmd:
            return FakeProc(0, json.dumps(full_sel))
        return FakeProc(7, json.dumps(TRIGGER_SEL))

    assert rm.run_matrix(Args(full=True, dry_run=True), runner=runner) == 0
    assert "--full" in seen[0]
    sel = rm.compute_selection("origin/dev", True, rm.REPO_ROOT_DEFAULT, runner=runner)
    assert rm.pytest_targets(sel) == ["tests/"]


def _vcs(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def test_compute_selection_real_emitter_trigger_stops_naming_file(tmp_path):
    # M5: drive the REAL emitter against a scratch repo whose head commit
    # changes pytest.ini (a full-suite trigger).
    import shutil
    import pytest
    if shutil.which("git") is None:
        pytest.skip("git is not available; the real-emitter cell needs a scratch repo")
    repo = tmp_path / "work"
    (repo / "tests").mkdir(parents=True)
    (repo / "tools").mkdir()
    emitter = Path(rm.__file__).resolve().parent / "run_touched_tests.py"
    (repo / "tools" / "run_touched_tests.py").symlink_to(emitter)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (repo / "tests" / "test_guard.py").write_text("def test_guard():\n    pass\n", encoding="utf-8")
    (repo / "docs" / "internal").mkdir(parents=True)
    (repo / "docs" / "internal" / "PR-MERGE-POLICY.md").write_text(
        "## Owner merge\n"
        "4. **The seat's own run, on a scratch merge**\n"
        "   `tests/test_guard.py`\n"
        "5. **The PR's changed or added tests**\n",
        encoding="utf-8",
    )
    pm = repo / "src" / "engram" / "packaging"
    pm.mkdir(parents=True)
    (pm / "test-map.json").write_text(
        json.dumps({"schema_version": 1, "mappings": [], "unmapped": []}), encoding="utf-8")
    _vcs(repo, "init", "-b", "dev")
    _vcs(repo, "config", "user.email", "t@example.com")
    _vcs(repo, "config", "user.name", "T")
    _vcs(repo, "add", ".")
    _vcs(repo, "commit", "-m", "base")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    _vcs(repo, "commit", "-am", "change a trigger file")
    try:
        rm.compute_selection("HEAD~1", False, str(repo))
        assert False, "expected FullTriggerStop"
    except rm.FullTriggerStop as e:
        assert "pytest.ini" in str(e) and "--full" in str(e)


def test_pytest_targets_full_vs_selected_vs_empty():
    assert rm.pytest_targets({"full": True, "selected": []}) == ["tests/"]
    assert rm.pytest_targets({"full": False, "selected": ["tests/x.py"]}) == ["tests/x.py"]
    assert rm.pytest_targets({"full": False, "selected": []}) == []


def test_compute_image_hash_deterministic_and_input_sensitive(tmp_path):
    # build a tiny fake repo with correct paths
    (tmp_path / "docker").mkdir()
    df = tmp_path / "docker" / "Dockerfile.full-default"
    df.write_text("FROM python:3.12\n")
    (tmp_path / "src" / "build" / "packaging").mkdir(parents=True)
    (tmp_path / "src" / "build" / "packaging" / "tiers.json").write_text("{}")
    (tmp_path / "requirements").mkdir()
    req = tmp_path / "requirements" / "requirements.txt"
    req.write_text("pytest\n")
    env = rm.P0_ENVS[0]
    h1 = rm.compute_image_hash(env, str(tmp_path))
    h2 = rm.compute_image_hash(env, str(tmp_path))
    assert h1 == h2  # deterministic
    df.write_text("FROM python:3.12\nRUN echo changed\n")
    h3 = rm.compute_image_hash(env, str(tmp_path))
    assert h3 != h1  # Dockerfile change invalidates
    (tmp_path / "src" / "build" / "packaging" / "tiers.json").write_text('{"changed": true}')
    h4 = rm.compute_image_hash(env, str(tmp_path))
    assert h4 != h3  # tiers.json change also invalidates
    req.write_text("pytest\nnumpy\n")
    h5 = rm.compute_image_hash(env, str(tmp_path))
    assert h5 != h4  # requirements change also invalidates


def test_compute_image_hash_warns_on_missing_input(tmp_path):
    import warnings
    # repo with no Dockerfile — should warn, not silently hash <absent>
    (tmp_path / "src" / "build" / "packaging").mkdir(parents=True)
    (tmp_path / "src" / "build" / "packaging" / "tiers.json").write_text("{}")
    env = rm.P0_ENVS[0]
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        rm.compute_image_hash(env, str(tmp_path))
    missing_warns = [x for x in w if "freshness input missing" in str(x.message)]
    assert missing_warns, "expected a warning for the missing Dockerfile"


def test_parse_pytest_counts():
    assert rm.parse_pytest_counts("2 failed, 5 passed in 0.1s") == (5, 2, 0)
    assert rm.parse_pytest_counts("10 passed in 1.2s") == (10, 0, 0)
    assert rm.parse_pytest_counts("1 failed, 2 passed, 1 error in 0.3s") == (2, 1, 1)
    assert rm.parse_pytest_counts("no summary here") == (0, 0, 0)


def test_aggregate_exit_precedence():
    R = rm.EnvResult
    assert rm.aggregate_exit([R("a", "pass"), R("b", "pass")]) == 0
    assert rm.aggregate_exit([R("a", "pass"), R("b", "fail")]) == 1
    # excluded outranks fail (it's the silent-skip class — must be loudest)
    assert rm.aggregate_exit([R("a", "fail"), R("b", "excluded")]) == 2


# ---------------------------------------------------------------------------
# Image freshness / exclusion
# ---------------------------------------------------------------------------
def test_ensure_image_fresh_no_build():
    runner, _ = fresh_runner()
    usable, reason = rm.ensure_image(rm.P0_ENVS[1], rm.REPO_ROOT_DEFAULT, False, runner=runner)
    assert usable and reason == "fresh"


def test_ensure_image_stale_no_build_excluded():
    runner, _ = make_runner(stamp="STALEHASH")
    usable, reason = rm.ensure_image(rm.P0_ENVS[1], rm.REPO_ROOT_DEFAULT, False, runner=runner)
    assert not usable and "stale" in reason and "--build" in reason


def test_ensure_image_absent_no_build_excluded():
    runner, _ = make_runner(stamp=None)
    usable, reason = rm.ensure_image(rm.P0_ENVS[1], rm.REPO_ROOT_DEFAULT, False, runner=runner)
    assert not usable and "absent" in reason


def test_ensure_image_stale_with_build_rebuilds():
    runner, _ = make_runner(stamp="STALEHASH", build_rc=0)
    usable, reason = rm.ensure_image(rm.P0_ENVS[1], rm.REPO_ROOT_DEFAULT, True, runner=runner)
    assert usable and reason == "rebuilt"


def test_ensure_image_build_failure_excluded():
    runner, _ = make_runner(stamp=None, build_rc=1)
    usable, reason = rm.ensure_image(rm.P0_ENVS[1], rm.REPO_ROOT_DEFAULT, True, runner=runner)
    assert not usable and "build FAILED" in reason


# ---------------------------------------------------------------------------
# Per-env run
# ---------------------------------------------------------------------------
def test_run_in_env_pass():
    runner, _ = make_runner(pytest_rc=0, pytest_out="5 passed in 0.2s")
    r = rm.run_in_env(rm.P0_ENVS[0], ["tests/test_a.py"], rm.REPO_ROOT_DEFAULT, runner=runner)
    assert r.status == "pass" and r.returncode == 0 and r.passed == 5


def test_run_in_env_fail_captures_tail():
    runner, _ = make_runner(pytest_rc=1, pytest_out="boom\n2 failed, 3 passed in 0.2s")
    r = rm.run_in_env(rm.P0_ENVS[0], ["tests/test_a.py"], rm.REPO_ROOT_DEFAULT, runner=runner)
    assert r.status == "fail" and r.returncode == 1 and r.failed == 2 and r.detail


def test_run_in_env_no_targets_is_skip():
    runner, _ = make_runner()
    r = rm.run_in_env(rm.P0_ENVS[0], [], rm.REPO_ROOT_DEFAULT, runner=runner)
    assert r.status == "skip" and r.returncode == 0


# ---------------------------------------------------------------------------
# End-to-end via main()/run_matrix() with docker mocked
# ---------------------------------------------------------------------------
class Args:
    def __init__(self, **kw):
        self.envs = "p0"; self.image = None; self.base = "origin/dev"
        self.full = False; self.build = False; self.dry_run = False; self.repo_root = None
        self.__dict__.update(kw)


def test_run_matrix_all_pass(monkeypatch):
    monkeypatch.setattr(rm, "docker_available", lambda: True)
    runner, _ = fresh_runner(pytest_rc=0, pytest_out="3 passed in 0.1s")
    assert rm.run_matrix(Args(), runner=runner) == 0


def test_run_matrix_one_env_fails(monkeypatch):
    monkeypatch.setattr(rm, "docker_available", lambda: True)
    # all images fresh, but pytest fails inside every container
    runner, _ = fresh_runner(pytest_rc=1, pytest_out="1 failed in 0.1s")
    assert rm.run_matrix(Args(), runner=runner) == 1


def test_run_matrix_excluded_image_exit_2(monkeypatch):
    monkeypatch.setattr(rm, "docker_available", lambda: True)
    # stale stamp + no --build → every image excluded → exit 2 (loud, not skip)
    runner, _ = make_runner(stamp="STALE", pytest_rc=0)
    assert rm.run_matrix(Args(build=False), runner=runner) == 2


def test_run_matrix_docker_absent_exit_3(monkeypatch):
    monkeypatch.setattr(rm, "docker_available", lambda: False)
    runner, _ = make_runner()
    assert rm.run_matrix(Args(), runner=runner) == 3


def test_run_matrix_dry_run_no_docker_needed(monkeypatch):
    # dry-run must not require docker and must exit 0 (plan only)
    monkeypatch.setattr(rm, "docker_available", lambda: False)
    runner, _ = make_runner()
    assert rm.run_matrix(Args(dry_run=True), runner=runner) == 0


def test_run_matrix_selection_failure_propagates(monkeypatch):
    # A failing selection emitter must NOT silently run the wrong suite —
    # run_matrix lets the RuntimeError propagate (main() converts it to exit 3).
    monkeypatch.setattr(rm, "docker_available", lambda: True)
    runner, _ = make_runner(selection_rc=1)
    try:
        rm.run_matrix(Args(), runner=runner)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError as e:
        assert "emitter exited 1" in str(e)


def test_main_selection_failure_returns_exit_3(monkeypatch):
    # End-to-end: emitter failure → RuntimeError → main()'s guard → exit 3.
    def boom(*a, **k):
        raise RuntimeError("selection emitter exited 1: emit boom")
    monkeypatch.setattr(rm, "compute_selection", boom)
    monkeypatch.setattr(rm, "docker_available", lambda: True)
    assert rm.main(["--envs", "p0"]) == 3


def test_resolve_envs_unknown_image_raises():
    try:
        rm.resolve_envs(Args(image=["does-not-exist"]))
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "unknown image" in str(e)


def test_resolve_envs_p0_is_four():
    assert len(rm.resolve_envs(Args())) == 4
