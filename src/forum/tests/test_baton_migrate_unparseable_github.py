"""#3377: baton-migrate-to-coord-store against the server's unparseable-github refusal.

Same contract as #3374's anchorless rows: a legacy row carrying a JUNK ``github``
FAILs (HTTP 422, the rule's text visible in the FAIL line), is counted, the run
continues, and ``main`` exits 1. The tool NEVER strips or rewrites the anchor --
the row is refused whole, nothing is stored. An anchorless bunch (no ``github``)
is legitimate and imports.

Over a tmp dir: one valid anchored PR baton, one bunch with junk github, one
anchorless bunch -> (imported 2, skipped 0, failed 1). Driven through the REAL
Flask app via an adapter with ForumClient's ``post`` shape. Environment-independent
(``World`` pins the pool sentinel). Identities and repo names are SYNTHETIC.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import World, AUTHOR, COLLEAGUE, SENTINEL, REPO  # noqa: E402

TOOLS = Path(__file__).resolve().parents[2] / "engram" / "tools"
sys.path.append(str(TOOLS))  # append, never insert(0): tools/ must not outrank src/ packages (#2063 guard)
from forum_api import ForumHttpError  # noqa: E402

MIGRATE_PATH = TOOLS / "baton-migrate-to-coord-store.py"


def _load():
    spec = importlib.util.spec_from_file_location("baton_migrate_3377", MIGRATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FlaskClient:
    def __init__(self, world):
        self.world = world

    def post(self, path, payload):
        r = self.world.client.post(path, json=payload)
        if r.status_code >= 300:
            raise ForumHttpError(r.status_code, r.get_data(as_text=True))
        return r.get_json()


def _baton(project, github=None):
    gh = f"github: {github}\n" if github else ""
    return (
        "---\n"
        f"project: {project}\n"
        f"title: {project} title\n"
        "status: in-progress\n"
        f"turn: {AUTHOR}\n"
        "turn_reason: migrated\n"
        f"participants: [{AUTHOR}, {COLLEAGUE}, {SENTINEL}]\n"
        f"{gh}"
        "---\nbody\n"
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def projects_dir(tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "PR-77.md").write_text(_baton("PR-77", f"pr/{REPO}/77"))
    (d / "DESIGN-junk.md").write_text(_baton("DESIGN-junk", "garbage"))
    (d / "DESIGN-free.md").write_text(_baton("DESIGN-free"))
    return d


def test_junk_github_row_fails_counted_run_continues_never_rewritten(world, projects_dir, capsys):
    from baton_gate import GITHUB_ANCHOR_ACCEPTED_FORMS as FORMS
    mod = _load()
    imported, skipped, failed = mod.migrate(projects_dir, _FlaskClient(world), AUTHOR, False)
    assert (imported, skipped, failed) == (2, 0, 1)
    # the junk row was refused WHOLE: not stored, not stored-with-the-anchor-stripped
    assert world.store.read_project("DESIGN-junk") is None
    assert world.store.read_project("PR-77") is not None
    assert world.store.read_project("DESIGN-free") is not None
    err = capsys.readouterr().err
    fail_lines = [ln for ln in err.splitlines() if ln.lstrip().startswith("FAIL")]
    assert len(fail_lines) == 1
    line = fail_lines[0]
    assert "DESIGN-junk" in line and "HTTP 422" in line
    assert FORMS in line                      # the 422 body is visible in the FAIL line


def test_main_exits_1_when_a_junk_row_failed(world, projects_dir, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "ForumClient", lambda url: _FlaskClient(world))
    monkeypatch.setattr(sys, "argv", [
        "baton-migrate", "--projects-dir", str(projects_dir),
        "--forum-url", "http://unused", "--agent", AUTHOR])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 1
    assert "failed 1" in capsys.readouterr().out
    assert world.store.read_project("DESIGN-junk") is None


def test_mixed_case_legacy_anchor_is_imported_stored_normalised(world, tmp_path):
    """The tool sends a legacy row's github verbatim; the SERVER normalises it (#3384 fold),
    so a mixed-case 'Project/7' lands as 'project/7' that every reader parses."""
    from forum.coordination.markdown import parse_frontmatter
    d = tmp_path / "legacy_case"
    d.mkdir()
    (d / "DESIGN-case.md").write_text(_baton("DESIGN-case", "Project/7"))
    mod = _load()
    assert mod.migrate(d, _FlaskClient(world), AUTHOR, False) == (1, 0, 0)
    fm = parse_frontmatter(world.store.read_project("DESIGN-case"))[0]
    assert fm["github"] == "project/7"


def test_tool_posts_the_junk_value_verbatim_never_strips(projects_dir):
    """The tool must hand the server the row's github UNCHANGED (the server decides)."""
    mod = _load()
    seen = {}

    class _Spy:
        def post(self, path, payload):
            seen[payload["project_id"]] = payload["github"]
            return {"seq": 1}

    mod.migrate(projects_dir, _Spy(), AUTHOR, False)
    assert seen["DESIGN-junk"] == "garbage"
    assert seen["DESIGN-free"] is None
