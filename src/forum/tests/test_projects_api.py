"""Tests for /api/projects/* endpoints — uses a FileStore via tmp_path for real write coverage.

Route-layer concerns (validation, response shape, 503/400/404 guards). The
writer-fn logic itself is unit-tested in test_coordination_projects.py.
"""

import sqlite3
import pytest

from forum.coordination import SeqAllocator, init as _init
from forum.coordination.store_file import FileStore
from forum.db import init_db
from forum import server as server_mod
from forum.server import create_app


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()

    store = FileStore(tmp_path / "coord")
    allocator = SeqAllocator(recover=store.recover_max_seq)
    # seed one baton for mutation tests
    _init(store, allocator, "PR-42",
          title="test pr", status="in-progress", turn="ariadne",
          participants=["ariadne", "luria"], turn_reason="start")

    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    application.config["COORD_STORE"] = store
    application.config["COORD_ALLOCATOR"] = allocator
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# GET /api/projects — list
# ---------------------------------------------------------------------------

class TestProjectsList:
    def test_returns_200(self, client):
        resp = client.get("/api/projects")
        assert resp.status_code == 200

    def test_returns_seeded_project(self, client):
        data = client.get("/api/projects").get_json()
        assert "projects" in data
        ids = [p["project_id"] for p in data["projects"]]
        assert "PR-42" in ids

    def test_project_fields_present(self, client):
        data = client.get("/api/projects").get_json()
        proj = next(p for p in data["projects"] if p["project_id"] == "PR-42")
        for field in ("project_id", "title", "status", "turn", "turn_since", "turn_reason", "participants", "seq", "github"):
            assert field in proj, f"missing field: {field}"

    def test_agent_filter(self, client):
        data = client.get("/api/projects?agent=ariadne").get_json()
        assert all("ariadne" in p["participants"] for p in data["projects"])

    def test_agent_filter_excludes_nonmember(self, client):
        data = client.get("/api/projects?agent=borges").get_json()
        # borges is not a participant in PR-42, so the list should be empty
        assert data["projects"] == []

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.get("/api/projects").status_code == 503


# ---------------------------------------------------------------------------
# GET /api/projects/<pid> — single project
# ---------------------------------------------------------------------------

class TestProjectShow:
    def test_returns_200_for_existing(self, client):
        resp = client.get("/api/projects/PR-42")
        assert resp.status_code == 200

    def test_returns_raw_content(self, client):
        data = client.get("/api/projects/PR-42").get_json()
        assert "raw" in data
        assert "PR-42" in data["raw"]

    def test_returns_project_id_in_response(self, client):
        data = client.get("/api/projects/PR-42").get_json()
        assert data["project_id"] == "PR-42"

    def test_returns_404_for_missing(self, client):
        resp = client.get("/api/projects/MISSING")
        assert resp.status_code == 404

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.get("/api/projects/PR-42").status_code == 503


# ---------------------------------------------------------------------------
# POST /api/projects — init
# ---------------------------------------------------------------------------

class TestProjectInit:
    def test_creates_new_project(self, client):
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-99",
            "title": "New Baton",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria", "ariadne"],
            "turn_reason": "start",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert "seq" in data
        assert data["project_id"] == "PR-99"

    def test_409_on_duplicate(self, client):
        # PR-42 already seeded
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-42",
            "title": "Dup",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria"],
            "turn_reason": "dup",
        })
        assert resp.status_code == 409

    def test_400_on_missing_fields(self, client):
        # missing project_id
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "title": "No ID",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria"],
            "turn_reason": "r",
        })
        assert resp.status_code == 400

    def test_400_on_invalid_agent(self, client):
        resp = client.post("/api/projects", json={
            "agent": "a+b",
            "project_id": "PR-100",
            "title": "t",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria"],
            "turn_reason": "r",
        })
        assert resp.status_code == 400

    def test_init_threads_agent_into_turn_by(self, client, app):
        # #1812: the endpoint's `agent` (the API caller) becomes turn_by —
        # verified via the store's parsed records since the list/show
        # responses don't (yet) surface turn_by.
        client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-102",
            "title": "t",
            "status": "in-progress",
            "turn": "ariadne",  # by_agent (luria) != turn (ariadne)
            "participants": ["luria", "ariadne"],
            "turn_reason": "r",
        })
        store = app.config["COORD_STORE"]
        rec = next(r for r in store.read_projects() if r.project_id == "PR-102")
        assert rec.turn_by == "luria"

    def test_init_persists_author_field(self, client, app):
        """#2961: an explicit `author` in the init payload is persisted
        verbatim as the `author:` frontmatter field -- read via raw
        frontmatter parsing since `author` is not (yet) a ProjectRecord
        field, matching how `colleague` is already handled."""
        from forum.coordination.markdown import parse_frontmatter

        client.post("/api/projects", json={
            "agent": "clio", "project_id": "PR-103", "title": "t",
            "status": "in-progress", "turn": "clio",
            "participants": ["clio", "ariadne"], "turn_reason": "start",
            "colleague": "ariadne", "author": "clio",
        })
        store = app.config["COORD_STORE"]
        raw = store.read_project("PR-103")
        fields, _ = parse_frontmatter(raw)
        assert fields.get("author") == "clio"

    def test_init_without_author_omits_field(self, client, app):
        """No `author` in the init payload -- pre-#2961 shape -- must not
        write an `author:` line at all (not an empty-string one), matching
        `colleague`'s own already-established omit-when-absent behavior."""
        from forum.coordination.markdown import parse_frontmatter

        client.post("/api/projects", json={
            "agent": "luria", "project_id": "PR-104", "title": "t",
            "status": "in-progress", "turn": "luria",
            "participants": ["luria"], "turn_reason": "start",
        })
        store = app.config["COORD_STORE"]
        raw = store.read_project("PR-104")
        fields, _ = parse_frontmatter(raw)
        assert "author" not in fields

    def test_flip_never_touches_author_field(self, client, app):
        """#2961's whole premise depends on `author` staying fixed across
        turn-changes, unlike `turn_by` (which flip() deliberately
        overwrites every time). Flip the baton twice and confirm `author`
        is byte-identical to what init wrote, throughout."""
        from forum.coordination.markdown import parse_frontmatter

        client.post("/api/projects", json={
            "agent": "clio", "project_id": "PR-105", "title": "t",
            "status": "in-progress", "turn": "clio",
            "participants": ["clio", "ariadne"], "turn_reason": "start",
            "colleague": "ariadne", "author": "clio",
        })
        store = app.config["COORD_STORE"]
        client.post("/api/projects/PR-105/flip", json={
            "agent": "clio", "to_agent": "ariadne", "reason": "please review",
        })
        client.post("/api/projects/PR-105/flip", json={
            "agent": "ariadne", "to_agent": "clio", "reason": "one nit, back to you",
        })
        fields, _ = parse_frontmatter(store.read_project("PR-105"))
        assert fields.get("author") == "clio"
        # turn_by, by contrast, MUST have moved on to the last flip's actor
        # -- the exact property that makes it unusable as an author proxy
        # past the first flip, and the reason #2961 needed a new field.
        # (turn_by is stored quoted, unlike author -- see projects.py's
        # flip()/init() comments on why -- so compare against the quoted form.)
        assert fields.get("turn_by") == '"ariadne"'

    def test_participants_as_string(self, client):
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-101",
            "title": "t",
            "status": "in-progress",
            "turn": "luria",
            "participants": "luria,ariadne",
            "turn_reason": "r",
        })
        assert resp.status_code == 201

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.post("/api/projects", json={"agent": "luria"}).status_code == 503

    def test_colleague_persists_and_renders_on_show(self, client):
        """#2764: colleague= at init is now actually persisted, not just
        used to shape participants + a client-side print."""
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-103",
            "title": "t",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria", "ariadne"],
            "turn_reason": "r",
            "colleague": "ariadne",
        })
        assert resp.status_code == 201
        raw = client.get("/api/projects/PR-103").get_json()["raw"]
        assert "colleague: ariadne" in raw

    def test_colleague_omitted_when_not_passed(self, client):
        client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-104",
            "title": "t",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria"],
            "turn_reason": "r",
        })
        raw = client.get("/api/projects/PR-104").get_json()["raw"]
        assert "colleague:" not in raw


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/flip
# ---------------------------------------------------------------------------

class TestProjectFlip:
    def test_flip_updates_turn(self, client):
        resp = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "please review",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert "seq" in data

    def test_flip_returns_seq(self, client):
        data = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "r",
        }).get_json()
        assert data["seq"] >= 1

    def test_flip_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "r",
        })
        assert resp.status_code == 404

    def test_flip_400_missing_to_agent(self, client):
        resp = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "reason": "r",
        })
        assert resp.status_code == 400

    def test_flip_400_missing_reason(self, client):
        resp = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
        })
        assert resp.status_code == 400

    def test_flip_threads_agent_into_turn_by(self, client, app):
        # #1812: the endpoint's `agent` (the API caller) becomes turn_by, which
        # may differ from to_agent (a third party flipping the baton onward).
        client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "please review",
        })
        store = app.config["COORD_STORE"]
        rec = next(r for r in store.read_projects() if r.project_id == "PR-42")
        assert rec.turn_by == "ariadne"

    def test_flip_returns_turn_and_status(self, client):
        data = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "colleague please",
        }).get_json()
        assert data["turn"] == "luria"
        assert data["status"] == "in-progress"


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/flip -- gate (#2884, second half)
#
# Covers the server-side mirror of tools/baton.py cmd_flip's CI-green /
# post-approval-freshness / colleague-approval gate. Uses the REAL loaded
# baton_gate module (via server_mod._load_baton_gate()) so these tests also
# double as a smoke test that the loader genuinely finds and loads the file
# in this repo's source-tree layout -- only the gh-shelling functions and
# the sentinel resolution are monkeypatched (no real `gh` calls, no real
# $ENGRAM_HOME I/O).
# ---------------------------------------------------------------------------

SENTINEL = "casey"


class TestProjectFlipGate:
    @pytest.fixture(autouse=True)
    def _pin_sentinel(self, monkeypatch):
        """Pin the pool sentinel for every test in this class, and reset
        the gate module's own cache before/after so no test leaks a
        resolved value into the next."""
        gate = server_mod._load_baton_gate()
        assert gate is not None, "baton_gate.py failed to load -- fix before trusting any gate test below"
        gate._POOL_SENTINEL_CACHE.clear()
        monkeypatch.setattr(gate, "_resolve_pool_sentinel", lambda *a, **kw: SENTINEL)
        yield
        gate._POOL_SENTINEL_CACHE.clear()

    def _seed(self, app, project_id, *, github, turn, colleague=None, author=None, participants=("borges", "sol")):
        store = app.config["COORD_STORE"]
        allocator = app.config["COORD_ALLOCATOR"]
        _init(
            store, allocator, project_id,
            title="gate test", status="in-progress", turn=turn,
            participants=list(participants), turn_reason="start",
            github=github, colleague=colleague, author=author,
        )

    # -- (a) maintainer-role flip on an UNQUALIFIED pr/<N> anchor: refused,
    #    matching #2884's original bug-report scenario exactly.
    def test_unqualified_anchor_maintainer_flip_refused(self, client, app):
        self._seed(app, "PR-2884a", github="pr/501", turn="borges")
        resp = client.post("/api/projects/PR-2884a/flip", json={
            "agent": "borges", "to_agent": SENTINEL, "reason": "ready",
        })
        assert resp.status_code in (400, 422)
        assert "repo-qualified" in resp.get_json()["error"]

    # -- (b) maintainer-role flip on a QUALIFIED anchor with CI red: refused.
    def test_qualified_anchor_ci_red_refused(self, client, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("red", "check 'tests' conclusion=FAILURE"))
        self._seed(app, "PR-2884b", github="pr/example-org/example-repo/502", turn="borges")
        resp = client.post("/api/projects/PR-2884b/flip", json={
            "agent": "borges", "to_agent": SENTINEL, "reason": "ready",
        })
        assert resp.status_code in (400, 422)
        assert "CI is red" in resp.get_json()["error"]

    # -- (c) CI green but no colleague approval (colleague participant
    #    exists): refused.
    def test_ci_green_no_colleague_approval_refused(self, client, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
        monkeypatch.setattr(gate, "_pr_approval_state", lambda pr, repo=None: ("no_approval", "no APPROVED review"))
        self._seed(
            app, "PR-2884c", github="pr/example-org/example-repo/503", turn="borges",
            colleague="sol", participants=("borges", "sol"),
        )
        resp = client.post("/api/projects/PR-2884c/flip", json={
            "agent": "borges", "to_agent": SENTINEL, "reason": "ready",
        })
        assert resp.status_code in (400, 422)
        assert "no colleague approval" in resp.get_json()["error"]

    # -- (d) CI green + colleague approval: succeeds (201).
    def test_ci_green_with_colleague_approval_succeeds(self, client, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
        monkeypatch.setattr(gate, "_pr_approval_state", lambda pr, repo=None: ("fresh", "approval covers tip"))
        self._seed(
            app, "PR-2884d", github="pr/example-org/example-repo/504", turn="borges",
            colleague="sol", participants=("borges", "sol"),
        )
        resp = client.post("/api/projects/PR-2884d/flip", json={
            "agent": "borges", "to_agent": SENTINEL, "reason": "ready",
        })
        assert resp.status_code == 201
        assert resp.get_json()["turn"] == SENTINEL

    # -- (e) non-maintainer-role flip (self-flip, or flip-to-colleague):
    #    proceeds ungated, same as cmd_flip -- and would FAIL if CI/approval
    #    were red, proving the gate genuinely did not run.
    def test_self_flip_proceeds_ungated(self, client, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("red", "would refuse if checked"))
        self._seed(app, "PR-2884e1", github="pr/example-org/example-repo/505", turn="borges")
        resp = client.post("/api/projects/PR-2884e1/flip", json={
            "agent": "borges", "to_agent": "borges", "reason": "still working",
        })
        assert resp.status_code == 201

    def test_flip_to_colleague_proceeds_ungated(self, client, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("red", "would refuse if checked"))
        self._seed(
            app, "PR-2884e2", github="pr/example-org/example-repo/506", turn="borges",
            colleague="sol", participants=("borges", "sol"),
        )
        resp = client.post("/api/projects/PR-2884e2/flip", json={
            "agent": "borges", "to_agent": "sol", "reason": "please review",
        })
        assert resp.status_code == 201

    # -- (e2) #2961: colleague sends the baton BACK TO THE AUTHOR after
    #    finding a blocker. PR-2926's real shape: author=clio, colleague=
    #    ariadne, current holder=ariadne (she took the seat to review).
    #    CI is deliberately left red / unmocked here -- if this were
    #    misclassified as a maintainer-role flip (the pre-#2961 bug), it
    #    would hit the CI-red refusal below and this test would fail,
    #    proving the exemption actually fires rather than merely existing.
    def test_colleague_flip_back_to_author_proceeds_ungated(self, client, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("red", "would refuse if checked"))
        self._seed(
            app, "PR-2926-repro", github="pr/example-org/example-repo/2926", turn="ariadne",
            colleague="ariadne", author="clio", participants=("clio", "ariadne"),
        )
        resp = client.post("/api/projects/PR-2926-repro/flip", json={
            "agent": "ariadne", "to_agent": "clio", "reason": "one blocker, back to author",
        })
        assert resp.status_code == 201
        assert resp.get_json()["turn"] == "clio"

    # -- (e3) same shape, but with NO author recorded (a baton created
    #    before #2961) -- must reproduce the OLD (buggy but documented)
    #    behavior exactly: refused, since the exemption has nothing to
    #    exempt with. Pins that "no author field" degrades to pre-#2961
    #    behavior rather than erroring or silently exempting everyone.
    def test_flip_to_agent_absent_from_turn_log_and_field_is_gated(self, client, app, monkeypatch):
        """`_seed` starts a baton directly at `turn="ariadne"` -- its own
        turn log's "initialized -> ariadne:" line names ariadne, not clio,
        so the legacy fallback correctly derives "ariadne" (not "clio") as
        the author-proxy here. Flipping to "clio" therefore still doesn't
        match ANY exemption (not current holder, not colleague, not the
        derived author) and stays gated -- the fallback recovers a REAL
        author signal when one exists in the log, it does not turn into a
        blanket bypass for every non-colleague flip target."""
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("red", "CI genuinely red"))
        self._seed(
            app, "PR-2926-no-author", github="pr/example-org/example-repo/2927", turn="ariadne",
            colleague="ariadne", author=None, participants=("clio", "ariadne"),
        )
        resp = client.post("/api/projects/PR-2926-no-author/flip", json={
            "agent": "ariadne", "to_agent": "clio", "reason": "one blocker, back to author",
        })
        assert resp.status_code in (400, 422)
        assert "CI is red" in resp.get_json()["error"]

    def test_legacy_baton_recovers_author_from_turn_log_without_backfill(self, client, app, monkeypatch):
        """#2961 follow-up, the actual gap Ariadne's round-2 review found:
        1,493 live batons predate the `author:` field. This is the FAITHFUL
        real-world sequence (unlike `_seed`'s direct-to-review-state
        shortcut above) -- init with turn=clio (the real author, no
        explicit `author=` passed), flip to ariadne for review, then
        ariadne flips it back to clio after a blocker. The fallback must
        recover "clio" from the turn log's own untouched first line and
        let this through with ZERO migration, ZERO write to the baton
        beyond the two ordinary flips every PR already does."""
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("red", "would refuse if checked"))
        store = app.config["COORD_STORE"]
        allocator = app.config["COORD_ALLOCATOR"]
        _init(
            store, allocator, "PR-2926-legacy",
            title="legacy repro", status="in-progress", turn="clio",
            participants=["clio", "ariadne"], turn_reason="start",
            github="pr/example-org/example-repo/2928", colleague="ariadne",
            # author intentionally omitted -- this IS the pre-#2961 shape.
        )
        r1 = client.post("/api/projects/PR-2926-legacy/flip", json={
            "agent": "clio", "to_agent": "ariadne", "reason": "please review",
        })
        assert r1.status_code == 201
        r2 = client.post("/api/projects/PR-2926-legacy/flip", json={
            "agent": "ariadne", "to_agent": "clio", "reason": "one blocker, back to author",
        })
        assert r2.status_code == 201
        assert r2.get_json()["turn"] == "clio"

    # -- (f) single-agent mode (no colleague participant): warns-and-proceeds.
    def test_single_agent_mode_no_colleague_warns_and_proceeds(self, client, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
        monkeypatch.setattr(gate, "_pr_approval_state", lambda pr, repo=None: ("no_approval", "no APPROVED review"))
        self._seed(
            app, "PR-2884f", github="pr/example-org/example-repo/507", turn="borges",
            colleague=None, participants=("borges",),
        )
        resp = client.post("/api/projects/PR-2884f/flip", json={
            "agent": "borges", "to_agent": SENTINEL, "reason": "ready",
        })
        assert resp.status_code == 201

    # -- gate-module-unavailable: a pr/-anchored maintainer flip is refused
    #    rather than silently let through (#2884's "unknown must not render
    #    as permitted" framing).
    def test_gate_module_unavailable_refuses_pr_anchored_maintainer_flip(self, client, app, monkeypatch):
        monkeypatch.setattr(server_mod, "_BATON_GATE_MODULE_CACHE", [None])
        self._seed(app, "PR-2884g", github="pr/example-org/example-repo/508", turn="borges")
        resp = client.post("/api/projects/PR-2884g/flip", json={
            "agent": "borges", "to_agent": SENTINEL, "reason": "ready",
        })
        assert resp.status_code in (400, 422)
        assert "gate unavailable" in resp.get_json()["error"]

    # -- gate-module-unavailable, but this baton was never pr/-anchored:
    #    module-independence -- proceeds normally, confirming the cheap
    #    prefix pre-check keeps a broken/undeployed gate module from
    #    blocking flips that were never gated in the first place.
    def test_gate_module_unavailable_non_pr_anchor_proceeds(self, client, app, monkeypatch):
        monkeypatch.setattr(server_mod, "_BATON_GATE_MODULE_CACHE", [None])
        self._seed(app, "PR-2884h", github="project/9", turn="borges")
        resp = client.post("/api/projects/PR-2884h/flip", json={
            "agent": "borges", "to_agent": SENTINEL, "reason": "ready",
        })
        assert resp.status_code == 201

    # -- mutation sanity: removing the gate call from api_project_flip (i.e.
    #    simulating pre-#2884-fix behavior) must make test (c) above fail --
    #    proving that test genuinely discriminates gated-vs-ungated. Modeled
    #    here by calling _check_flip_gate directly and asserting it, rather
    #    than mutating server.py at runtime.
    def test_check_flip_gate_directly_discriminates(self, app, monkeypatch):
        gate = server_mod._load_baton_gate()
        monkeypatch.setattr(gate, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
        monkeypatch.setattr(gate, "_pr_approval_state", lambda pr, repo=None: ("no_approval", "no APPROVED review"))
        self._seed(
            app, "PR-2884i", github="pr/example-org/example-repo/509", turn="borges",
            colleague="sol", participants=("borges", "sol"),
        )
        store = app.config["COORD_STORE"]
        result = server_mod._check_flip_gate(store, "PR-2884i", SENTINEL)
        assert result is not None
        message, status = result
        assert status in (400, 422)
        assert "no colleague approval" in message


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/claim
# ---------------------------------------------------------------------------

class TestProjectClaim:
    def test_claim_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/claim", json={
            "agent": "luria",
            "pool_sentinel": "lei",
        })
        assert resp.status_code == 201

    def test_claim_400_missing_pool_sentinel(self, client):
        resp = client.post("/api/projects/PR-42/claim", json={
            "agent": "luria",
        })
        assert resp.status_code == 400

    def test_claim_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/claim", json={
            "agent": "luria",
            "pool_sentinel": "lei",
        })
        assert resp.status_code == 404

    def test_claim_returns_turn_and_status(self, client):
        data = client.post("/api/projects/PR-42/claim", json={
            "agent": "luria",
            "pool_sentinel": "lei",
        }).get_json()
        assert data["turn"] == "luria"
        assert data["status"] == "in-progress"


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/release
# ---------------------------------------------------------------------------

class TestProjectRelease:
    def test_release_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
            "reason": "all done",
        })
        assert resp.status_code == 201

    def test_release_done_true(self, client):
        resp = client.post("/api/projects/PR-42/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
            "reason": "done",
            "done": True,
        })
        assert resp.status_code == 201
        # verify title was marked (done) via list endpoint
        data = client.get("/api/projects?active_only=false").get_json()
        proj = next((p for p in data["projects"] if p["project_id"] == "PR-42"), None)
        assert proj is not None
        assert "(done)" in proj["title"]

    def test_release_400_missing_reason(self, client):
        resp = client.post("/api/projects/PR-42/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
        })
        assert resp.status_code == 400

    def test_release_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
            "reason": "r",
        })
        assert resp.status_code == 404

    def test_release_returns_turn_and_status(self, client):
        data = client.post("/api/projects/PR-42/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
            "reason": "back to pool",
        }).get_json()
        assert data["turn"] == "lei"
        assert data["status"] == "in-progress"


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/status — close/reopen dispatcher
# ---------------------------------------------------------------------------

class TestProjectStatus:
    def test_close_dispatches_correctly(self, client):
        resp = client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "new_status": "cancelled",
            "reason": "scope dropped",
        })
        assert resp.status_code == 201

    def test_reopen_dispatches_for_active_status(self, client):
        # first close it
        client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "new_status": "cancelled",
            "reason": "closed first",
        })
        # now reopen
        resp = client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "new_status": "in-progress",
            "reason": "reopened",
        })
        assert resp.status_code == 201

    def test_status_400_missing_new_status(self, client):
        resp = client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "reason": "r",
        })
        assert resp.status_code == 400

    def test_status_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/status", json={
            "agent": "ariadne",
            "new_status": "merged",
            "reason": "r",
        })
        assert resp.status_code == 404

    def test_status_returns_updated_status(self, client):
        data = client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "new_status": "cancelled",
            "reason": "scope dropped",
        }).get_json()
        assert data["status"] == "cancelled"
        assert "turn" in data


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/rename
# ---------------------------------------------------------------------------

class TestProjectRename:
    def test_rename_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/rename", json={
            "agent": "ariadne",
            "new_title": "Renamed Baton",
        })
        assert resp.status_code == 201

    def test_rename_400_missing_new_title(self, client):
        resp = client.post("/api/projects/PR-42/rename", json={
            "agent": "ariadne",
        })
        assert resp.status_code == 400

    def test_rename_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/rename", json={
            "agent": "ariadne",
            "new_title": "X",
        })
        assert resp.status_code == 404

    def test_rename_returns_turn_and_status(self, client):
        data = client.post("/api/projects/PR-42/rename", json={
            "agent": "ariadne",
            "new_title": "Renamed",
        }).get_json()
        assert data["turn"] == "ariadne"
        assert data["status"] == "in-progress"


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/anchor
# ---------------------------------------------------------------------------

class TestProjectAnchor:
    def test_anchor_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/anchor", json={
            "agent": "ariadne",
            "github": "pr/42",
        })
        assert resp.status_code == 201

    def test_anchor_400_missing_github(self, client):
        resp = client.post("/api/projects/PR-42/anchor", json={
            "agent": "ariadne",
        })
        assert resp.status_code == 400

    def test_anchor_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/anchor", json={
            "agent": "ariadne",
            "github": "pr/99",
        })
        assert resp.status_code == 404

    def test_anchor_returns_turn_and_status(self, client):
        data = client.post("/api/projects/PR-42/anchor", json={
            "agent": "ariadne",
            "github": "pr/42",
        }).get_json()
        assert data["turn"] == "ariadne"
        assert data["status"] == "in-progress"


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/participants
# ---------------------------------------------------------------------------
# Seeded PR-42 participants: [ariadne, luria] (see the `app` fixture above).

class TestProjectAddParticipant:
    def test_add_participant_returns_201_and_added_true(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
            "participant": "borges",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["added"] is True
        assert "seq" in data

    def test_add_participant_idempotent_no_op_returns_added_false(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
            "participant": "ariadne",  # already a seeded participant
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["added"] is False

    def test_add_participant_403_when_agent_not_a_participant(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "casey",  # not in [ariadne, luria]
            "participant": "borges",
        })
        assert resp.status_code == 403

    def test_add_participant_400_missing_agent(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "participant": "borges",
        })
        assert resp.status_code == 400

    def test_add_participant_400_missing_participant(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
        })
        assert resp.status_code == 400

    def test_add_participant_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/participants", json={
            "agent": "ariadne",
            "participant": "borges",
        })
        assert resp.status_code == 404

    def test_add_participant_persists_in_subsequent_read(self, client):
        """The added participant is visible on a later GET (persisted, not just echoed).

        This is the API-layer half of the flip-unblock regression guard — the
        CLI-level half (baton flip actually SUCCEEDS post-add, the check that
        matters since 'not a participant' is enforced client-side in
        cmd_flip) lives in tests/test_baton.py.
        """
        add_resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
            "participant": "borges",
        })
        assert add_resp.status_code == 201

        show_resp = client.get("/api/projects/PR-42")
        assert show_resp.status_code == 200
        assert "borges" in show_resp.get_json()["raw"]


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/colleague  (#2764)
# ---------------------------------------------------------------------------
# Seeded PR-42 turn=ariadne, participants=[ariadne, luria] (see the `app` fixture above).

class TestProjectSetColleague:
    def test_set_colleague_returns_201_and_participant_added_true(self, client):
        resp = client.post("/api/projects/PR-42/colleague", json={
            "agent": "ariadne",
            "colleague": "borges",  # not yet a participant
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["participant_added"] is True
        assert "seq" in data

    def test_set_colleague_already_participant_returns_participant_added_false(self, client):
        resp = client.post("/api/projects/PR-42/colleague", json={
            "agent": "ariadne",
            "colleague": "luria",  # already a seeded participant
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["participant_added"] is False

    def test_set_colleague_by_participant_who_is_not_holder_succeeds(self, client):
        """luria is a participant but not the turn holder (ariadne) -- still allowed."""
        resp = client.post("/api/projects/PR-42/colleague", json={
            "agent": "luria",
            "colleague": "borges",
        })
        assert resp.status_code == 201

    def test_set_colleague_403_when_agent_neither_participant_nor_holder(self, client):
        resp = client.post("/api/projects/PR-42/colleague", json={
            "agent": "casey",  # not in [ariadne, luria], not the turn holder
            "colleague": "borges",
        })
        assert resp.status_code == 403

    def test_set_colleague_400_missing_agent(self, client):
        resp = client.post("/api/projects/PR-42/colleague", json={
            "colleague": "borges",
        })
        assert resp.status_code == 400

    def test_set_colleague_400_missing_colleague(self, client):
        resp = client.post("/api/projects/PR-42/colleague", json={
            "agent": "ariadne",
        })
        assert resp.status_code == 400

    def test_set_colleague_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/colleague", json={
            "agent": "ariadne",
            "colleague": "borges",
        })
        assert resp.status_code == 404

    def test_set_colleague_persists_and_renders_on_show(self, client):
        """The API-layer half of the #2764 fix: colleague: is now readable
        via a plain GET (which baton show just prints raw) -- pre-fix,
        nothing was ever persisted server-side for --colleague to render."""
        set_resp = client.post("/api/projects/PR-42/colleague", json={
            "agent": "ariadne",
            "colleague": "borges",
        })
        assert set_resp.status_code == 201

        show_resp = client.get("/api/projects/PR-42")
        assert show_resp.status_code == 200
        raw = show_resp.get_json()["raw"]
        assert "colleague: borges" in raw


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/gc
# ---------------------------------------------------------------------------

class TestProjectGc:
    def test_gc_closes_with_new_status(self, client):
        resp = client.post("/api/projects/PR-42/gc", json={
            "agent": "ariadne",
            "new_status": "merged",
            "reason": "pr merged on gh",
        })
        assert resp.status_code == 201

    def test_gc_400_missing_new_status(self, client):
        resp = client.post("/api/projects/PR-42/gc", json={
            "agent": "ariadne",
            "reason": "r",
        })
        assert resp.status_code == 400

    def test_gc_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/gc", json={
            "agent": "ariadne",
            "new_status": "merged",
            "reason": "r",
        })
        assert resp.status_code == 404

    def test_gc_returns_turn_and_status(self, client):
        data = client.post("/api/projects/PR-42/gc", json={
            "agent": "ariadne",
            "new_status": "merged",
            "reason": "pr merged on gh",
        }).get_json()
        assert data["status"] == "merged"
        assert "turn" in data


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/merge
# ---------------------------------------------------------------------------

class TestProjectMerge:
    def test_merge_returns_201_and_seq(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={
            "agent": "luria",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert "seq" in data
        assert data["seq"] >= 1

    def test_merge_returns_merged_status(self, client):
        data = client.post("/api/projects/PR-42/merge", json={
            "agent": "luria",
        }).get_json()
        assert data["status"] == "merged"
        assert "turn" not in data

    def test_merge_archives_baton(self, client, app):
        client.post("/api/projects/PR-42/merge", json={"agent": "luria"})
        # After merge, project should not appear in active list
        data = client.get("/api/projects").get_json()
        ids = [p["project_id"] for p in data["projects"]]
        assert "PR-42" not in ids

    def test_merge_forced(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={
            "agent": "luria",
            "forced": True,
        })
        assert resp.status_code == 201

    def test_merge_400_missing_agent(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={})
        assert resp.status_code == 400

    def test_merge_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/merge", json={
            "agent": "luria",
        })
        assert resp.status_code == 404

    def test_merge_400_invalid_agent(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={
            "agent": "a+b",
        })
        assert resp.status_code == 400

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.post("/api/projects/PR-42/merge", json={"agent": "luria"}).status_code == 503
