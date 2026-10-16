"""#3366: baton-migrate-to-coord-store against the server's anchorless-PR refusal.

The migration tool posts project_id/title/status/turn/github/participants with no
transition_phase and no bypass. Over a directory holding one anchorless PR-shaped
baton, one anchored PR baton and one bunch, the tool's own contract is:
the anchorless row prints ``FAIL ... HTTP 422 -- <the rule's text>`` and is
counted as failed; the run continues (the other two import); ``main`` exits 1
because ``failed`` is non-zero. Driven through the REAL Flask app via an adapter
that has ForumClient's ``post`` shape.

Identities and repo names are SYNTHETIC.
"""

import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location("baton_migrate_3366", MIGRATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FlaskClient:
    """ForumClient.post shape over the Flask test client."""

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
    (d / "PR-autopull2.md").write_text(_baton("PR-autopull2"))
    (d / "PR-77.md").write_text(_baton("PR-77", f"pr/{REPO}/77"))
    (d / "pool-bunch.md").write_text(_baton("pool-bunch", "project/7"))
    return d


def test_migrate_counts_422_as_failed_and_continues(world, projects_dir, capsys):
    mod = _load()
    imported, skipped, failed = mod.migrate(projects_dir, _FlaskClient(world), AUTHOR, False)
    assert (imported, skipped, failed) == (2, 0, 1)
    assert world.store.read_project("PR-autopull2") is None
    assert world.store.read_project("PR-77") is not None
    assert world.store.read_project("pool-bunch") is not None
    captured = capsys.readouterr()
    fail_lines = [ln for ln in captured.err.splitlines() if ln.lstrip().startswith("FAIL")]
    assert len(fail_lines) == 1
    line = fail_lines[0]
    assert "PR-autopull2" in line and "HTTP 422" in line
    # the rule's own text is visible in the FAIL line, both routes named
    assert world.gate.PR_ANCHOR_TWO_ROUTE_HINT in line


def test_main_exits_nonzero_when_any_row_failed(world, projects_dir, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "ForumClient", lambda url: _FlaskClient(world))
    monkeypatch.setattr(sys, "argv", [
        "baton-migrate", "--projects-dir", str(projects_dir),
        "--forum-url", "http://unused", "--agent", AUTHOR])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 1
    assert "failed 1" in capsys.readouterr().out
