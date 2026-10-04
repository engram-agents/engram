"""Contract tests for engram-alpha#3286 -- `project_id` path traversal on init.

Written BEFORE the implementation (contract-first). ``POST /api/projects`` built
the baton file path from the request-BODY ``project_id`` with no shape check, so
``../../escape`` created a file OUTSIDE the store.

Contract:
  S1  init validates ``project_id`` against the CLI's rule (``_PROJECT_ID_RE`` in
      ``src/engram/tools/baton.py``) with ``fullmatch`` on the value exactly as
      sent; failure is a 422 naming the route and field ``project_id``, before any
      store access, no seq consumed. Non-string => 422 (never 500). Null/absent
      keeps the existing 400 "required" answer.
  S2  every path the file store builds from a project id goes through ONE helper
      that raises ``InvalidProjectId`` (a ``ValueError``) for an id that cannot
      name a file directly under ``projects/`` (archive: ``archive/``), and that
      resolves symlinks: a symlinked baton file, or a symlinked ``projects/`` /
      ``archive/`` directory that leads outside the store, is refused. A
      symlinked store ROOT (an admin's deliberate layout) keeps working.
  S3  routes map the refusal: 404 on GET, 422 otherwise.
  Parity  the server's rule is the CLI's rule (one known difference, pinned).

Identities are SYNTHETIC.
"""

import hashlib
import os
import re
import sqlite3
from pathlib import Path

import pytest

from forum.coordination import SeqAllocator
from forum.coordination.store_file import FileStore
from forum.db import init_db
from forum.server import create_app

ROUTE = "POST /api/projects (init)"
AGENT = "ariadne"

TRAVERSAL_IDS = [
    "../x",
    "../../escape",
    "../../../../tmp/x",
    "/abs/path",
    "a/b",
    "a\\b",
    "%2e%2e/x",
    "..",
    ".",
    "PR-1/../../z",
    "PR-1\n",
    "PR-1 ",
    "",
    "PR",
    "-PR-1",
    "PR-1\x00",
    "PR-1\x00/../../n",
    "ＰＲ-1",   # fullwidth P R
    "Р-1",      # Cyrillic Er
    "PR-١",     # Arabic-indic digit one
]

NON_STRING_IDS = [["PR-1"], 5, {"a": "b"}, True, 1.5]

VALID_IDS = ["PR-42", "pool-42", "UMWELT-PR-925", "DESIGN-trust-tier-v2", "PR-92-paper"]

CONTENT = "---\nproject: x\n---\nbody\n"


def snapshot(root: Path):
    """Every path under ``root`` (dirs/files/symlinks, with content hashes; links NOT followed)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in dirnames:
            p = Path(dirpath, d)
            out[str(p.relative_to(root))] = ("link", os.readlink(p)) if p.is_symlink() else "dir"
        for fn in filenames:
            p = Path(dirpath, fn)
            if p.is_symlink():
                out[str(p.relative_to(root))] = ("link", os.readlink(p))
            else:
                out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Sandbox:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        # Deep store so `../../../..` still lands inside tmp_path.
        self.root = tmp_path / "a" / "b" / "c" / "coord"
        self.root.mkdir(parents=True)
        db_path = str(tmp_path / "forum.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        self.store = FileStore(self.root)
        self.allocator = SeqAllocator(recover=self.store.recover_max_seq)
        app = create_app(db_path, str(tmp_path / "audit.jsonl"))
        app.config["TESTING"] = True
        app.config["COORD_STORE"] = self.store
        app.config["COORD_ALLOCATOR"] = self.allocator
        self.client = app.test_client()


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


def init_body(pid, **over):
    body = {"agent": AGENT, "project_id": pid, "title": "t", "status": "in-progress",
            "turn": AGENT, "participants": [AGENT], "turn_reason": "x"}
    body.update(over)
    return body


class TestInitRefusesBadProjectIds:
    @pytest.mark.parametrize("pid", TRAVERSAL_IDS + NON_STRING_IDS, ids=lambda p: repr(p)[:40])
    def test_422_and_nothing_created_anywhere(self, sb, pid):
        before = snapshot(sb.tmp)
        seq_before = sb.allocator.current()
        resp = sb.client.post("/api/projects", json=init_body(pid))
        after = snapshot(sb.tmp)
        assert resp.status_code == 422, (resp.status_code, resp.get_json())
        msg = resp.get_json()["error"]
        assert ROUTE in msg and "project_id" in msg, msg
        assert {k: v for k, v in after.items() if k != "forum.db"} == \
               {k: v for k, v in before.items() if k != "forum.db"}, sorted(set(after) ^ set(before))
        assert sb.allocator.current() == seq_before

    def test_bad_id_is_refused_before_the_required_field_check(self, sb):
        # Other required fields missing too: the id verdict (422) comes first.
        resp = sb.client.post("/api/projects", json={"agent": AGENT, "project_id": "../x"})
        assert resp.status_code == 422

    def test_escape_shape_does_not_create_outside_the_store(self, sb):
        resp = sb.client.post("/api/projects", json=init_body("../../escape"))
        assert resp.status_code == 422
        assert not (sb.root / ".." / ".." / "escape.md").exists()
        assert not (sb.root.parent / "escape.md").exists()

    @pytest.mark.parametrize("pid", [None])
    def test_null_id_keeps_the_required_400(self, sb, pid):
        resp = sb.client.post("/api/projects", json=init_body(pid))
        assert resp.status_code == 400

    def test_absent_id_keeps_the_required_400(self, sb):
        body = init_body("PR-1")
        del body["project_id"]
        assert sb.client.post("/api/projects", json=body).status_code == 400


class TestValidIdsStillWork:
    @pytest.mark.parametrize("pid", VALID_IDS, ids=lambda p: p[:30])
    def test_init_ok(self, sb, pid):
        # #3366: a PR-shaped id must name its PR; a bunch anchor is the id-agnostic one.
        resp = sb.client.post("/api/projects", json=init_body(pid, github="project/7"))
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["project_id"] == pid
        assert (sb.store.projects_dir / f"{pid}.md").is_file()
        assert sb.client.get(f"/api/projects/{pid}").status_code == 200


STORE_BAD_IDS = ["../x", "../../escape", "/abs/path", "a/b", "a\\b", "", "PR-1\x00",
                 "PR-1/../../z", "..", "."]


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "a" / "b" / "coord"
    root.mkdir(parents=True)
    return FileStore(root), tmp_path


class TestStoreContainment:
    def test_invalid_project_id_is_a_value_error(self):
        from forum.coordination.names import InvalidProjectId
        assert issubclass(InvalidProjectId, ValueError)

    @pytest.mark.parametrize("pid", STORE_BAD_IDS + [None, 5, b"PR-1"], ids=lambda p: repr(p)[:30])
    def test_read_refused(self, store, pid):
        s, tmp = store
        with pytest.raises(ValueError):
            s.read_project(pid)

    @pytest.mark.parametrize("pid", STORE_BAD_IDS, ids=lambda p: repr(p)[:30])
    def test_write_refused_and_creates_nothing(self, store, pid):
        s, tmp = store
        before = snapshot(tmp)
        with pytest.raises(ValueError):
            s.write_project(pid, CONTENT, seq=1)
        assert snapshot(tmp) == before

    @pytest.mark.parametrize("pid", STORE_BAD_IDS, ids=lambda p: repr(p)[:30])
    def test_archive_refused(self, store, pid):
        s, tmp = store
        s.projects_dir.mkdir(parents=True, exist_ok=True)
        (s.root / "x.md").write_text("victim")
        (s.root / "escape.md").write_text("victim2")
        before = snapshot(tmp)
        with pytest.raises(ValueError):
            s.archive_project(pid)
        assert snapshot(tmp) == before

    def test_archive_of_valid_id_still_lands_in_archive_dir(self, store):
        s, tmp = store
        s.write_project("PR-7", CONTENT, seq=1)
        s.archive_project("PR-7")
        assert s.read_project("PR-7") is None
        assert (s.root / "archive" / "PR-7.md").is_file()

    def test_archive_collision_suffix_stays_in_archive(self, store):
        s, tmp = store
        for _ in range(2):
            s.write_project("PR-7", CONTENT, seq=1)
            s.archive_project("PR-7")
        assert sorted(p.name for p in (s.root / "archive").iterdir()) == ["PR-7.1.md", "PR-7.md"]

    def test_non_cli_shaped_but_safe_ids_still_work_at_store_level(self, store):
        # The store guards CONTAINMENT, not the CLI shape (existing callers use short ids).
        s, tmp = store
        s.write_project("x", CONTENT, seq=1)
        assert s.read_project("x") is not None


def _symlink(link: Path, target: Path):
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


class TestSymlinks:
    """Policy: a symlinked baton FILE is refused outright (even one pointing inside
    projects/); a symlinked projects/ or archive/ DIRECTORY must resolve to
    <resolved root>/projects (resp. archive); a symlinked store ROOT is fine."""

    def test_symlinked_baton_file_read_refused(self, store):
        s, tmp = store
        outside = tmp / "elsewhere" / "x.md"
        outside.parent.mkdir()
        outside.write_text("SECRET")
        _symlink(s.projects_dir / "PR-1.md", outside)
        with pytest.raises(ValueError):
            s.read_project("PR-1")

    def test_symlinked_baton_file_write_refused_and_outside_untouched(self, store):
        s, tmp = store
        outside = tmp / "elsewhere" / "x.md"
        outside.parent.mkdir()
        outside.write_text("SECRET")
        _symlink(s.projects_dir / "PR-1.md", outside)
        before = snapshot(tmp)
        with pytest.raises(ValueError):
            s.write_project("PR-1", CONTENT, seq=1)
        assert outside.read_text() == "SECRET"
        assert snapshot(tmp) == before

    def test_dangling_symlinked_baton_file_write_refused(self, store):
        s, tmp = store
        target = tmp / "elsewhere" / "new.md"
        target.parent.mkdir()
        _symlink(s.projects_dir / "PR-1.md", target)
        with pytest.raises(ValueError):
            s.write_project("PR-1", CONTENT, seq=1)
        assert not target.exists()

    def test_symlink_to_sibling_inside_projects_also_refused(self, store):
        s, tmp = store
        s.write_project("PR-2", CONTENT, seq=1)
        _symlink(s.projects_dir / "PR-1.md", s.projects_dir / "PR-2.md")
        with pytest.raises(ValueError):
            s.read_project("PR-1")

    def test_symlinked_projects_dir_to_outside_refused(self, store):
        s, tmp = store
        outside = tmp / "elsewhere"
        outside.mkdir()
        _symlink(s.root / "projects", outside)
        before = snapshot(tmp)
        with pytest.raises(ValueError):
            s.write_project("PR-1", CONTENT, seq=1)
        with pytest.raises(ValueError):
            s.read_project("PR-1")
        assert snapshot(tmp) == before
        assert list(outside.iterdir()) == []

    def test_symlinked_archive_dir_to_outside_refused(self, store):
        s, tmp = store
        s.write_project("PR-1", CONTENT, seq=1)
        outside = tmp / "elsewhere"
        outside.mkdir()
        _symlink(s.root / "archive", outside)
        with pytest.raises(ValueError):
            s.archive_project("PR-1")
        assert list(outside.iterdir()) == []
        assert (s.projects_dir / "PR-1.md").is_file()  # not moved

    def test_symlinked_archive_target_file_refused(self, store):
        s, tmp = store
        s.write_project("PR-1", CONTENT, seq=1)
        outside = tmp / "elsewhere" / "o.md"
        outside.parent.mkdir()
        outside.write_text("SECRET")
        _symlink(s.root / "archive" / "PR-1.md", outside)
        with pytest.raises(ValueError):
            s.archive_project("PR-1")
        assert outside.read_text() == "SECRET"
        assert (s.projects_dir / "PR-1.md").is_file()

    def test_symlinked_store_root_still_works(self, tmp_path):
        # The admin deliberately symlinks the WHOLE store root: both sides resolve.
        real = tmp_path / "real" / "coord"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        s = FileStore(link)
        s.write_project("PR-1", CONTENT, seq=1)
        assert s.read_project("PR-1") is not None
        s.archive_project("PR-1")
        assert (real / "archive" / "PR-1.md").is_file()
        assert s.read_project("PR-1") is None


class TestRoutesMapTheRefusal:
    def test_symlinked_projects_dir_get_is_404_post_is_422_never_500(self, sb):
        outside = sb.tmp / "elsewhere"
        outside.mkdir()
        sb.root.mkdir(parents=True, exist_ok=True)
        _symlink(sb.root / "projects", outside)
        assert sb.client.get("/api/projects/PR-1").status_code == 404
        resp = sb.client.post("/api/projects", json=init_body("PR-1"))
        assert resp.status_code == 422, resp.get_json()
        assert list(outside.iterdir()) == []

    def test_symlinked_baton_file_via_routes(self, sb):
        outside = sb.tmp / "elsewhere" / "x.md"
        outside.parent.mkdir()
        outside.write_text("SECRET")
        _symlink(sb.store.projects_dir / "PR-9.md", outside)
        assert sb.client.get("/api/projects/PR-9").status_code == 404
        resp = sb.client.post("/api/projects/PR-9/flip",
                              json={"agent": AGENT, "to_agent": AGENT, "reason": "r"})
        assert resp.status_code in (404, 422), resp.status_code
        assert outside.read_text() == "SECRET"

    def test_url_pid_dotdot_is_not_500(self, sb):
        for pid in ("..", "%2e%2e", "a%5Cb", "x%00y"):
            resp = sb.client.get(f"/api/projects/{pid}")
            assert resp.status_code in (404, 422), (pid, resp.status_code)


# ---------------------------------------------------------------------------
# Parity with the CLI's rule
# ---------------------------------------------------------------------------

def _cli_pattern() -> str:
    path = Path(__file__).resolve().parents[2] / "engram" / "tools" / "baton.py"
    src = path.read_text(encoding="utf-8")
    m = re.search(r'^_PROJECT_ID_RE = re\.compile\(r"([^"\n]*)"\)', src, re.M)
    assert m, "could not find _PROJECT_ID_RE in the CLI"
    return m.group(1)


class TestParityWithCli:
    def test_pattern_strings_are_identical(self):
        from forum.coordination.names import PROJECT_ID_RE
        assert PROJECT_ID_RE.pattern == _cli_pattern()

    def test_same_verdict_on_a_corpus_under_fullmatch(self):
        from forum.coordination.names import is_valid_project_id
        cli = re.compile(_cli_pattern())
        corpus = [p for p in TRAVERSAL_IDS if isinstance(p, str)] + VALID_IDS + [
            "PR-1", "a-b", "A-1", "1-A", "PR--", "PR-_", "PR-a_b-c", "x", "PR-1.5", "PR-1?",
            "PR-" + "a" * 300]
        for pid in corpus:
            assert is_valid_project_id(pid) == bool(cli.fullmatch(pid)), repr(pid)

    def test_known_difference_trailing_newline_pinned(self):
        # The CLI uses .match + `$`, which accepts one trailing newline; the server
        # does not (the id becomes a file name). We do NOT edit the CLI.
        from forum.coordination.names import is_valid_project_id
        cli = re.compile(_cli_pattern())
        assert cli.match("PR-1\n") is not None
        assert is_valid_project_id("PR-1\n") is False

    def test_dot_is_not_allowed(self):
        from forum.coordination.names import is_valid_project_id
        assert is_valid_project_id("PR-1.5") is False
        assert is_valid_project_id("PR-.") is False

    def test_non_strings_are_invalid(self):
        from forum.coordination.names import is_valid_project_id
        for v in (None, ["PR-1"], 5, b"PR-1"):
            assert is_valid_project_id(v) is False


class TestOverlongButValidShapedId:
    def test_300_char_id_is_a_422_not_a_500(self, sb):
        # The CLI rule has no length cap, so this id is CLI-shaped; it fails at the
        # filesystem (ENAMETOOLONG). That must read as a bad id, not a server fault.
        pid = "PR-" + "a" * 300
        # #3366: anchored (bunch) so the refusal under test is the filesystem one,
        # not the anchorless-PR rule.
        resp = sb.client.post("/api/projects", json=init_body(pid, github="project/7"))
        assert resp.status_code == 422, (resp.status_code, resp.get_data(as_text=True)[:200])
        assert "project_id" in resp.get_json()["error"]
