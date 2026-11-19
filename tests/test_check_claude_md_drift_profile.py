"""The CLAUDE.md drift tool diffs the profile the home RECORDED, not always the full template.

Contract (config.json `claude_md_profile`; no record = full):
  - a local home reports NO drift against the local template and never reports the full
    template's extra sections as missing; a change to the full template alone is not local drift;
  - a full home (record "full", null, or none) behaves as before;
  - an unrecognised recorded value is "undetermined": exit 2, a message, nothing compared,
    and no fallback to the full template.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = REPO_ROOT / "src" / "engram" / "tools" / "check-claude-md-drift.py"

_LOCAL = "# Identity\n\nLocal paragraph one.\n\nLocal paragraph two.\n"
_FULL = _LOCAL + "\n## Full only section\n\nFull-only paragraph about fairies.\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "src_repo"
    d = r / "src" / "engram" / "templates"
    d.mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.invalid")
    _git(r, "config", "user.name", "T")
    (d / "template.CLAUDE.md").write_text(_FULL, encoding="utf-8")
    (d / "template.CLAUDE.local.md").write_text(_LOCAL, encoding="utf-8")
    _commit(r, "base")
    return r


def _home(tmp_path: Path, record) -> Path:
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    cfg = h / "config.json"
    if cfg.exists():
        cfg.unlink()
    if record is not None:
        cfg.write_text(json.dumps(record), encoding="utf-8")
    return h


def _run(*args: str):
    p = subprocess.run([sys.executable, str(CLI_PATH), *args], capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def _live(repo, home, live_text, tmp_path):
    live = tmp_path / "live.md"
    live.write_text(live_text, encoding="utf-8")
    return _run("--repo", str(repo), "--head", "HEAD", "--live", str(live), "--engram-home", str(home))


def _two_ref(repo, home):
    return _run("--repo", str(repo), "--base", "HEAD~1", "--head", "HEAD", "--engram-home", str(home))


# ---- live mode -------------------------------------------------------------

def test_local_home_live_no_drift_and_no_full_sections_missing(repo, tmp_path):
    home = _home(tmp_path, {"claude_md_profile": "local"})
    rc, out, err = _live(repo, home, _LOCAL, tmp_path)
    assert rc == 0, (out, err)
    assert "Full only" not in out and "fairies" not in out
    assert "0 missing" in out


def test_full_home_live_still_reports_full_sections_missing(repo, tmp_path):
    for record in (None, {}, {"claude_md_profile": "full"}, {"claude_md_profile": None}):
        home = _home(tmp_path, record)
        rc, out, _ = _live(repo, home, _LOCAL, tmp_path)
        assert rc == 1, record
        assert "Full-only paragraph" in out, record


def test_local_home_live_reports_a_missing_local_paragraph(repo, tmp_path):
    home = _home(tmp_path, {"claude_md_profile": "local"})
    rc, out, _ = _live(repo, home, "# Identity\n\nLocal paragraph one.\n", tmp_path)
    assert rc == 1 and "Local paragraph two" in out


# ---- two-ref mode ----------------------------------------------------------

def test_local_home_ignores_a_full_only_template_change(repo, tmp_path):
    t = repo / "src" / "engram" / "templates" / "template.CLAUDE.md"
    t.write_text(_FULL + "\nMore full text.\n", encoding="utf-8")
    _commit(repo, "full change")
    assert _two_ref(repo, _home(tmp_path, {"claude_md_profile": "local"}))[0] == 0
    rc, out, _ = _two_ref(repo, _home(tmp_path, None))
    assert rc == 1 and "More full text" in out


def test_local_home_sees_a_local_template_change(repo, tmp_path):
    t = repo / "src" / "engram" / "templates" / "template.CLAUDE.local.md"
    t.write_text(_LOCAL + "\nNew local text.\n", encoding="utf-8")
    _commit(repo, "local change")
    rc, out, _ = _two_ref(repo, _home(tmp_path, {"claude_md_profile": "local"}))
    assert rc == 1 and "New local text" in out
    assert _two_ref(repo, _home(tmp_path, None))[0] == 0


# ---- undetermined ----------------------------------------------------------

@pytest.mark.parametrize("record", [{"claude_md_profile": "tiny"}, {"claude_md_profile": "lean"}, {"claude_md_profile": 3}, ["not", "a", "dict"]])
def test_unrecognised_record_is_undetermined_in_both_modes(repo, tmp_path, record):
    home = _home(tmp_path, record)
    t = repo / "src" / "engram" / "templates" / "template.CLAUDE.md"
    t.write_text(_FULL + "\nMore full text.\n", encoding="utf-8")
    _commit(repo, "full change")
    for rc, out, err in (_two_ref(repo, home), _live(repo, home, _LOCAL, tmp_path)):
        assert rc == 2
        assert "undetermined" in err and "ask the user" in err.lower()
        assert out == ""  # nothing compared, no fallback to the full template


def test_unreadable_config_is_undetermined(repo, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{not json", encoding="utf-8")
    rc, out, err = _two_ref(repo, home)
    assert rc == 2 and "undetermined" in err and out == ""


# ---- one rule, two readers --------------------------------------------------

def test_tool_and_upgrade_flow_read_the_same_profile(tmp_path):
    import importlib.util

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    template_render = _load("template_render_parity", REPO_ROOT / "src" / "engram" / "template_render.py")
    flows_src = (REPO_ROOT / "src" / "build" / "engine" / "flows.py").read_text(encoding="utf-8")
    # flows.py has package-relative imports; lift only the profile reader out of it.
    start = flows_src.index("_CLAUDE_MD_PROFILE_KEY =")
    end = flows_src.index("def _claude_md_template_rel")
    ns: dict = {}
    exec("import json, os\n" + flows_src[start:end], ns)  # noqa: S102 - our own source text
    flows_reader = ns["_recorded_claude_md_profile"]

    cases = [None, "{not json", "[1]", "{}", '{"claude_md_profile": null}', '{"claude_md_profile": "full"}',
             '{"claude_md_profile": "local"}', '{"claude_md_profile": "tiny"}', '{"claude_md_profile": 3}',
             '{"claude_md_profile": "Local"}', '{"claude_md_profile": "local "}',
             '{"claude_md_profile": "lean"}']
    for i, raw in enumerate(cases):
        h = tmp_path / f"h{i}"
        h.mkdir()
        if raw is not None:
            (h / "config.json").write_text(raw, encoding="utf-8")
        assert template_render.recorded_claude_md_profile(str(h)) == flows_reader(str(h)), raw
    assert template_render.recorded_claude_md_profile("") == flows_reader("") == "full"
    # Case-variant and padded values are not "local", and the retired working name "lean" is no
    # alias for it: both readers must refuse them the same way.
    for i, raw in enumerate(cases):
        if raw and ("Local" in raw or "local \"" in raw or "lean" in raw):
            h = tmp_path / f"h{i}"
            assert template_render.recorded_claude_md_profile(str(h)) == flows_reader(str(h)) == "undetermined", raw
