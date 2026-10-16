"""Contract tests for the viz Moments page (engram-alpha#3511).

Written BEFORE the implementation. Covers: viz.data.moments.load_moments
(read-only, grouping, ordering, malformed-data tolerance), the /api/moments
and /moments routes (hide-when-empty redirect, agent resolution), the
conditional nav tab, and DOM-safe rendering of agent-written text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import server as viz_server
from viz import shell as viz_shell

_STATIC = Path(viz_shell.__file__).resolve().parent / "static"


def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY, type TEXT, claim TEXT, status TEXT,
            is_current INTEGER DEFAULT 1, metadata TEXT, trust_tier TEXT,
            recall_summary TEXT)"""
    )
    return conn


def _person(conn, pid, name, relation=None, moments=None, tier="primary_user",
            is_current=1, status="active", raw_metadata=None):
    if raw_metadata is None:
        meta = {"name": name, "aliases": [], "special_moments": moments or [],
                "special_moment_cursor": 3}
        if relation:
            meta["relation"] = relation
        raw_metadata = json.dumps(meta)
    conn.execute(
        "INSERT INTO nodes (id,type,claim,status,is_current,metadata,trust_tier)"
        " VALUES (?,?,?,?,?,?,?)",
        (pid, "person", f"{name} — {relation}" if relation else name, status,
         is_current, raw_metadata, tier),
    )


def _node(conn, nid, claim, summary=None, is_current=1, status="active"):
    conn.execute(
        "INSERT INTO nodes (id,type,claim,status,is_current,metadata,recall_summary)"
        " VALUES (?,?,?,?,?,?,?)",
        (nid, "observation_factual", claim, status, is_current, "{}", summary),
    )


def _mom(desc, node_id="ob_0001", added="2026-01-01T00:00:00Z"):
    return {"description": desc, "node_id": node_id, "added_at": added}


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="engram_viz_moments_")
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "knowledge.db")
        self.conn = _make_db(self.db)

    def commit(self):
        self.conn.commit()
        self.conn.close()


class TestLoadMoments(_DbCase):
    def _load(self):
        from viz.data import moments
        self.commit()
        return moments.load_moments(self.db)

    def test_read_only_and_cursor_untouched(self):
        from viz.data import moments
        _person(self.conn, "pn_0001", "Lei", "collaborator",
                [_mom("Goodnight", "ob_0001")])
        _node(self.conn, "ob_0001", "claim", "summary")
        self.commit()
        before_meta = sqlite3.connect(self.db).execute(
            "SELECT metadata FROM nodes WHERE id='pn_0001'").fetchone()[0]
        before_hash = hashlib.sha256(Path(self.db).read_bytes()).hexdigest()
        seen = []
        real = sqlite3.connect

        def spy(*a, **k):
            seen.append((a, k))
            return real(*a, **k)

        with unittest.mock.patch.object(sqlite3, "connect", spy):
            moments.load_moments(self.db)
        after_meta = sqlite3.connect(self.db).execute(
            "SELECT metadata FROM nodes WHERE id='pn_0001'").fetchone()[0]
        self.assertEqual(before_meta, after_meta)
        self.assertEqual(before_hash,
                         hashlib.sha256(Path(self.db).read_bytes()).hexdigest())
        self.assertIn('"special_moment_cursor": 3', after_meta)
        self.assertTrue(seen, "load_moments must open the DB via sqlite3.connect")
        for a, k in seen:
            self.assertIn("mode=ro", str(a[0]))
            self.assertTrue(k.get("uri"))
            # The path must be percent-encoded: the URI must equal the
            # quoted form exactly (an unquoted f-string would not).
            self.assertEqual(
                str(a[0]), Path(self.db).absolute().as_uri() + "?mode=ro")

    def test_special_char_path_stays_read_only(self):
        from viz.data import moments
        with tempfile.TemporaryDirectory() as td:
            for bad in ("q?x", "q#x"):
                d = Path(td) / bad
                d.mkdir()
                db = str(d / "k.db")
                conn = _make_db(db)
                _person(conn, "pn_0001", "Lei", "c", [_mom("Hi", "ob_0001")])
                conn.commit()
                conn.close()
                before = {str(p) for p in Path(td).rglob("*")}
                self.assertEqual(len(moments.load_moments(db)), 1)
                self.assertTrue(moments.has_moments(db))
                self.assertEqual({str(p) for p in Path(td).rglob("*")}, before)

    def test_group_shape_and_person_fields(self):
        _person(self.conn, "pn_0001", "Lei", "collaborator",
                [_mom("Goodnight", "ob_0001")], tier="primary_user")
        _node(self.conn, "ob_0001", "the claim", "the summary")
        groups = self._load()
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual((g["person_id"], g["name"], g["relation"], g["trust_tier"]),
                         ("pn_0001", "Lei", "collaborator", "primary_user"))
        self.assertEqual(g["moments"], [{
            "description": "Goodnight", "added_at": "2026-01-01T00:00:00Z",
            "node_id": "ob_0001", "node_summary": "the summary",
            "node_exists": True}])

    def _relation_for(self, **meta_extra):
        meta = {"name": "Lei", "aliases": [], "special_moment_cursor": 3,
                "special_moments": [_mom("m", "ob_0001")]}
        meta.update(meta_extra)
        _person(self.conn, "pn_0001", "Lei", raw_metadata=json.dumps(meta))
        _node(self.conn, "ob_0001", "claim", "summary")
        return self._load()[0]["relation"]

    def test_relation_key_used(self):
        self.assertEqual(self._relation_for(relation="collaborator"), "collaborator")

    def test_legacy_role_key_is_the_fallback(self):
        # Persons created before the role->relation rename carry only "role".
        self.assertEqual(self._relation_for(role="collaborator"), "collaborator")

    def test_relation_wins_over_role(self):
        self.assertEqual(
            self._relation_for(relation="friend", role="collaborator"), "friend")

    def test_neither_or_empty_relation_is_none(self):
        self.assertIsNone(self._relation_for())

    def test_empty_strings_are_none(self):
        self.assertIsNone(self._relation_for(relation="", role="  "))

    def test_summary_falls_back_to_truncated_claim(self):
        _person(self.conn, "pn_0001", "Lei", None, [_mom("m", "ob_0001")])
        _node(self.conn, "ob_0001", "x" * 400, None)
        m = self._load()[0]["moments"][0]
        self.assertTrue(m["node_exists"])
        self.assertTrue(0 < len(m["node_summary"]) <= 170)
        self.assertTrue(m["node_summary"].startswith("xxxx"))

    def test_missing_or_noncurrent_or_retracted_node_still_shown(self):
        _person(self.conn, "pn_0001", "Lei", None, [
            _mom("gone", "ob_9999", "2026-01-04"),
            _mom("old", "ob_0002", "2026-01-03"),
            _mom("retracted", "ob_0003", "2026-01-02"),
            _mom("nonid", None, "2026-01-01"),
        ])
        _node(self.conn, "ob_0002", "c", "s", is_current=0)
        _node(self.conn, "ob_0003", "c", "s", status="retracted")
        ms = self._load()[0]["moments"]
        self.assertEqual([m["description"] for m in ms],
                         ["gone", "old", "retracted", "nonid"])
        for m in ms:
            self.assertFalse(m["node_exists"])
            self.assertIsNone(m["node_summary"])

    def test_only_current_nonretracted_persons(self):
        _person(self.conn, "pn_0001", "Old", None, [_mom("a")], is_current=0)
        _person(self.conn, "pn_0002", "Gone", None, [_mom("b")], status="retracted")
        _person(self.conn, "pn_0003", "Empty", None, [])
        _person(self.conn, "pn_0004", "Live", None, [_mom("c")])
        groups = self._load()
        self.assertEqual([g["person_id"] for g in groups], ["pn_0004"])

    def test_ordering_newest_first_within_and_across_groups(self):
        _person(self.conn, "pn_0001", "A", None, [
            _mom("a-old", added="2026-01-01"), _mom("a-new", added="2026-03-01")])
        _person(self.conn, "pn_0002", "B", None, [
            _mom("b-mid", added="2026-02-01"), _mom("b-newest", added="2026-04-01")])
        groups = self._load()
        self.assertEqual([g["person_id"] for g in groups], ["pn_0002", "pn_0001"])
        self.assertEqual([m["description"] for m in groups[0]["moments"]],
                         ["b-newest", "b-mid"])
        self.assertEqual([m["description"] for m in groups[1]["moments"]],
                         ["a-new", "a-old"])

    def test_malformed_data_never_crashes_and_good_moments_survive(self):
        _person(self.conn, "pn_0001", "BadJson", None, raw_metadata="{not json")
        _person(self.conn, "pn_0002", "NotList", None,
                raw_metadata=json.dumps({"name": "NotList", "special_moments": "x"}))
        _person(self.conn, "pn_0003", "NotDict", None,
                raw_metadata=json.dumps({"name": "NotDict",
                                         "special_moments": [1, "s", None]}))
        _person(self.conn, "pn_0004", "Mixed", "friend", [
            {"description": "", "node_id": "ob_0001"},
            {"node_id": "ob_0001"},
            {"description": "   "},
            {"description": 42},
            "string entry",
            _mom("kept", "ob_0001", "2026-05-05"),
        ])
        _person(self.conn, "pn_0005", "NullMeta", None, raw_metadata="{}")
        self.conn.execute("UPDATE nodes SET metadata=NULL WHERE id='pn_0005'")
        _person(self.conn, "pn_0006", "ListMeta", None, raw_metadata="[1,2]")
        groups = self._load()
        self.assertEqual([g["person_id"] for g in groups], ["pn_0004"])
        self.assertEqual([m["description"] for m in groups[0]["moments"]], ["kept"])

    def test_missing_added_at_sorts_last_and_does_not_crash(self):
        _person(self.conn, "pn_0001", "A", None, [
            {"description": "undated"}, _mom("dated", added="2026-01-01")])
        ms = self._load()[0]["moments"]
        self.assertEqual([m["description"] for m in ms], ["dated", "undated"])

    def test_no_moments_anywhere_is_empty_list(self):
        _person(self.conn, "pn_0001", "A", None, [])
        self.assertEqual(self._load(), [])

    def test_missing_db_file_is_empty_list(self):
        from viz.data import moments
        self.assertEqual(moments.load_moments("/nonexistent/knowledge.db"), [])
        self.assertFalse(moments.has_moments("/nonexistent/knowledge.db"))

    def _assert_has_agrees(self, expected):
        from viz.data import moments
        self.conn.commit()
        self.assertEqual(bool(moments.load_moments(self.db)), expected)
        self.assertEqual(moments.has_moments(self.db), expected)

    def test_has_moments_no_moments(self):
        _person(self.conn, "pn_0001", "A", None, [])
        self._assert_has_agrees(False)

    def test_has_moments_one_valid_moment(self):
        _person(self.conn, "pn_0001", "A", None, [_mom("x")])
        self._assert_has_agrees(True)

    def test_has_moments_only_malformed(self):
        _person(self.conn, "pn_0001", "BadJson", None, raw_metadata="{not json")
        _person(self.conn, "pn_0002", "Mixed", None, [
            {"description": ""}, {"node_id": "ob_0001"}, "s", {"description": 42}])
        self._assert_has_agrees(False)

    def test_has_moments_retracted_person_with_moments(self):
        _person(self.conn, "pn_0001", "Gone", None, [_mom("a")], status="retracted")
        _person(self.conn, "pn_0002", "Old", None, [_mom("a")], is_current=0)
        self._assert_has_agrees(False)

    def test_has_moments_short_circuits_before_building_groups(self):
        from viz.data import moments
        for i in range(3):
            _person(self.conn, f"pn_000{i}", f"P{i}", None, [_mom("x")])
        self.conn.commit()
        calls = []
        real = moments._valid_moments

        def spy(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with unittest.mock.patch.object(moments, "_valid_moments", spy):
            self.assertTrue(moments.has_moments(self.db))
        self.assertEqual(len(calls), 1)


class _LiveBase(_DbCase):
    def setUp(self):
        super().setUp()
        self._saved_agents = viz_agents.AGENTS
        self._saved_default = viz_agents.DEFAULT_AGENT
        self.addCleanup(setattr, viz_agents, "AGENTS", self._saved_agents)
        self.addCleanup(setattr, viz_agents, "DEFAULT_AGENT", self._saved_default)
        self.empty_db = str(Path(self.tmp.name) / "empty.db")
        c = _make_db(self.empty_db)
        c.commit()
        c.close()
        _person(self.conn, "pn_0001", "Lei", "collaborator",
                [_mom("<script>alert(1)</script>", "ob_0001")])
        _node(self.conn, "ob_0001", "c", "s")
        self.commit()
        viz_agents.AGENTS = {
            "full": {"name": "full", "label": "Full", "db": self.db},
            "empty": {"name": "empty", "label": "Empty", "db": self.empty_db},
        }
        viz_agents.DEFAULT_AGENT = "full"
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.shutdown)

    def _get(self, path, follow=True):
        url = f"http://127.0.0.1:{self.port}{path}"

        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        try:
            if follow:
                with urlopen(url, timeout=5) as r:
                    return r.status, r.read(), dict(r.headers)
            with build_opener(_NoRedirect).open(url, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})


class TestRoutes(_LiveBase):
    def test_api_returns_groups_for_agent(self):
        st, body, _ = self._get("/api/moments?agent=full")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d["agent"], "full")
        self.assertEqual(d["groups"][0]["name"], "Lei")
        self.assertEqual(d["groups"][0]["moments"][0]["description"],
                         "<script>alert(1)</script>")

    def test_api_default_agent_and_empty_agent(self):
        d = json.loads(self._get("/api/moments")[1])
        self.assertEqual(d["agent"], "full")
        d = json.loads(self._get("/api/moments?agent=empty")[1])
        self.assertEqual(d["groups"], [])

    def test_api_unknown_agent_matches_graph_convention(self):
        g = json.loads(self._get("/api/graph?agent=nope")[1])
        st, body, _ = self._get("/api/moments?agent=nope")
        m = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(m["error"], g["error"])
        self.assertEqual(m["agent"], "nope")
        self.assertEqual(m["groups"], [])

    def test_page_served_when_moments_exist(self):
        st, body, _ = self._get("/moments?agent=full")
        self.assertEqual(st, 200)
        html = body.decode()
        self.assertIn("Moments your agent chose to keep", html)
        self.assertIn("/static/moments.js", html)
        self.assertNotIn("<script>alert(1)", html)

    def test_page_redirects_to_root_when_empty_preserving_agent(self):
        st, _, h = self._get("/moments?agent=empty", follow=False)
        self.assertEqual(st, 302)
        self.assertEqual(h["Location"], "/?agent=empty")

    def test_page_redirects_for_unknown_agent(self):
        st, _, h = self._get("/moments?agent=nope", follow=False)
        self.assertEqual(st, 302)
        self.assertEqual(h["Location"], "/?agent=nope")

    def test_redirect_without_agent_param_has_no_query(self):
        viz_agents.DEFAULT_AGENT = "empty"
        st, _, h = self._get("/moments", follow=False)
        self.assertEqual(st, 302)
        self.assertEqual(h["Location"], "/")

    def test_nav_tab_present_only_with_moments(self):
        for page in ("/", "/health", "/stats", "/config", "/moments"):
            html = self._get(f"{page}?agent=full")[1].decode()
            self.assertIn(">Moments</a>", html, page)
            self.assertIn('href="/moments?agent=full"', html, page)
        for page in ("/", "/health", "/stats", "/config"):
            html = self._get(f"{page}?agent=empty")[1].decode()
            self.assertNotIn("Moments</a>", html, page)
            self.assertNotIn("/moments", html, page)

    def test_other_tabs_unchanged_when_empty(self):
        html = self._get("/?agent=empty")[1].decode()
        for label in ("Graph View", "Health Dashboard", "Stats", "Config"):
            self.assertIn(f">{label}</a>", html)

    def test_nav_check_uses_has_moments_with_short_timeout(self):
        seen = []
        real = viz_server.viz_moments_data.has_moments

        def spy(db, **kw):
            seen.append(kw)
            return real(db, **kw)

        with unittest.mock.patch.object(
                viz_server.viz_moments_data, "has_moments", spy):
            self._get("/?agent=full")
        self.assertTrue(seen)
        self.assertEqual(seen[0].get("timeout"), 0.5)

    def test_nav_check_failure_hides_tab_and_logs(self):
        def boom(db, **kw):
            raise sqlite3.OperationalError("database is locked")

        with unittest.mock.patch.object(
                viz_server.viz_moments_data, "has_moments", boom), \
                self.assertLogs(level="WARNING"):
            html = self._get("/?agent=full")[1].decode()
        self.assertNotIn("Moments</a>", html)

    def test_render_nav_default_has_no_moments(self):
        self.assertNotIn("Moments", viz_shell._render_nav("graph", agent="x"))


class TestDomSafety(unittest.TestCase):
    def setUp(self):
        self.js = (_STATIC / "moments.js").read_text(encoding="utf-8")

    def test_no_raw_html_sinks(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write", "createContextualFragment"):
            self.assertNotIn(sink, self.js, sink)

    def test_text_goes_through_textcontent(self):
        self.assertIn("textContent", self.js)
        self.assertIn("/api/moments", self.js)


if __name__ == "__main__":
    unittest.main()
