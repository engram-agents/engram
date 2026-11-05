"""#3901: baton-migrate-to-coord-store against the server's born-open rule.

Init refuses ``merged`` / ``cancelled`` with a 422 (a baton is born open), so the
migration tool must not POST a legacy file whose own status is closed. Such a file
prints one SKIP line, is counted in a separate ``closed`` tally (not skipped, not
failed), creates nothing, and does not make the run fail. Dry-run reports the same
SKIP lines. Driven through the REAL Flask app, same harness as
test_baton_migrate_anchorless_pr.py.

Identities and repo names are SYNTHETIC.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import World, AUTHOR, COLLEAGUE, SENTINEL  # noqa: E402

TOOLS = Path(__file__).resolve().parents[2] / "engram" / "tools"
sys.path.append(str(TOOLS))  # append, never insert(0): tools/ must not outrank src/ packages (#2063 guard)
from forum_api import ForumHttpError  # noqa: E402

MIGRATE_PATH = TOOLS / "baton-migrate-to-coord-store.py"


def _load():
    spec = importlib.util.spec_from_file_location("baton_migrate_3901", MIGRATE_PATH)
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


def _baton(project, status):
    return (
        "---\n"
        f"project: {project}\n"
        f"title: {project} title\n"
        f"status: {status}\n"
        f"turn: {AUTHOR}\n"
        "turn_reason: migrated\n"
        f"participants: [{AUTHOR}, {COLLEAGUE}, {SENTINEL}]\n"
        "---\nbody\n"
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def projects_dir(tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "done-one.md").write_text(_baton("done-one", "merged"))
    (d / "dropped-one.md").write_text(_baton("dropped-one", "cancelled"))
    (d / "live-one.md").write_text(_baton("live-one", "in-progress"))
    return d


def _skip_line(pid, status):
    return (f"  SKIP  {pid}: closed ({status}); closed batons are not imported "
            f"— a baton is born open (#3901)")


def test_closed_legacy_batons_are_skipped_not_failed(world, projects_dir, capsys):
    mod = _load()
    tally = {}
    imported, skipped, failed = mod.migrate(
        projects_dir, _FlaskClient(world), AUTHOR, False, tally=tally)
    assert (imported, tally["closed"], failed) == (1, 2, 0)
    assert skipped == 0
    captured = capsys.readouterr()
    out_lines = captured.out.splitlines()
    assert _skip_line("done-one", "merged") in out_lines
    assert _skip_line("dropped-one", "cancelled") in out_lines
    assert "FAIL" not in captured.err
    assert world.store.read_project("done-one") is None
    assert world.store.read_project("dropped-one") is None
    assert world.store.read_project("live-one") is not None


def test_dry_run_reports_the_same_skip_lines(world, projects_dir, capsys):
    mod = _load()
    tally = {}
    imported, _skipped, failed = mod.migrate(
        projects_dir, _FlaskClient(world), AUTHOR, True, tally=tally)
    assert (imported, tally["closed"], failed) == (1, 2, 0)
    out_lines = capsys.readouterr().out.splitlines()
    assert _skip_line("done-one", "merged") in out_lines
    assert _skip_line("dropped-one", "cancelled") in out_lines


def test_main_summary_has_a_closed_tally_and_exits_zero(world, projects_dir, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "ForumClient", lambda url: _FlaskClient(world))
    monkeypatch.setattr(sys, "argv", [
        "baton-migrate", "--projects-dir", str(projects_dir),
        "--forum-url", "http://unused", "--agent", AUTHOR])
    mod.main()  # a closed skip is not a failure: no SystemExit
    assert "Summary: imported 1, skipped 0, closed 2, failed 0" in capsys.readouterr().out


def test_the_tools_closed_set_matches_the_servers():
    """The tool keeps a local copy of the closed set (it must run without the forum
    package); this pins the copy to the set the server's init route refuses."""
    from forum.coordination.store_file import CLOSED_STATUSES as server_closed

    assert _load().CLOSED_STATUSES == frozenset(server_closed)
