"""Tests for tools/study_queue.py -- the read-only study queue (engram-alpha#3919, item M3).

The tool prints this seat's top N study targets: one node id or file reference, why it is on
the list, and ONE deterministic method string, drawn (in priority order) from the latest dream
report, the weakest load-bearing belief, open questions, live conjectures and pending cascades.

Every cell builds its own small store on a copy of a REAL schema (created once through the real
server's _get_db, never a hand-rolled CREATE TABLE) and inserts rows with raw SQL, like
tests/test_weakest_load_bearing.py, whose shape this file copies.  Fixture ids are NOT in the
real ENGRAM id format (tests/test_scan_leaks.py bans those): they look like ``qux-001``.
Each cell's docstring names the mutation that must redden it, where one exists.
"""

import atexit
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_owner import FakeOwner, registered_sql  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "study_queue.py"
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_read_registry as rr  # noqa: E402

FLAG_KEY = "study_queue_reads_via_owner"
SITE_PREFIX = "tools/study_queue.py:"
NOW = "2026-10-19T12:00:00Z"

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_study_queue_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TEMPLATE = {}


def _template_db() -> Path:
    """One real-schema, empty knowledge.db, built once through the real server."""
    if "path" in _TEMPLATE:
        return _TEMPLATE["path"]
    home = Path(TEST_DIR) / "template_home"
    shutil.rmtree(home, ignore_errors=True)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    import server

    server._configure_paths(str(home))
    server._ensure_data_dir()
    conn = server._get_db()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    _TEMPLATE["path"] = home / "knowledge.db"
    return _TEMPLATE["path"]


class Graph:
    """A fixture graph on a private copy of the real schema."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "fixture.db"
        shutil.copyfile(_template_db(), self.path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row

    def node(self, nid, ntype="derivation", conf=0.8, *, claim=None, current=1,
             status="active", importance=0.05, recall_turn=0):
        self.conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, confidence, is_current, "
            "status, importance_score, recall_turn) VALUES (?,?,?,?,?,?,?,?,?)",
            (nid, ntype, claim if claim is not None else f"claim of {nid}",
             "2026-01-01T00:00:00", conf, current, status, importance, recall_turn),
        )
        return nid

    def edge(self, src, tgt, rel="derives_from"):
        self.conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?,?,?,?)", (src, tgt, rel, "2026-01-01T00:00:00"))

    def dependents(self, target, k):
        for i in range(k):
            did = f"d_{target}_{i}"
            self.node(did, conf=0.9, importance=0.9)
            self.edge(did, target)

    def marker(self, node_id, status="pending", event="ev1"):
        self.conn.execute(
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, "
            "status, created_at) VALUES (?,?,?,?,?,?)",
            (node_id, event, "rootX", "supersede", status, "2026-01-02T00:00:00"))

    def question(self, nid, *, recall_turn=0, status="open", current=1, claim=None):
        return self.node(nid, "question", None, claim=claim, current=current, status=status,
                         recall_turn=recall_turn)

    def conjecture(self, nid, conf, *, status="active", current=1, claim=None):
        return self.node(nid, "conjecture", conf, claim=claim, current=current, status=status)

    def filler(self):
        """Ten 0.8-confidence nodes with two dependents each (score 0.4 apiece)."""
        for i in range(10):
            fid = f"fill_{i:02d}"
            self.node(fid, conf=0.8, importance=0.9)
            self.dependents(fid, 2)

    def commit(self):
        self.conn.commit()


def _rich_graph(g, order=None):
    """Every source has candidates, plus a decoy for every filter."""
    steps = [
        lambda: (g.filler(), g.node("weak", conf=0.3, claim="the weak belief"),
                 g.dependents("weak", 3)),
        lambda: (g.question("qux-001", recall_turn=9, claim="late question"),
                 g.question("qux-002", recall_turn=2, claim="early question B"),
                 g.question("qux-003", recall_turn=2, claim="early question A"),
                 g.question("qux-004", recall_turn=50, claim="a fourth question, cut off")),
        lambda: (g.question("qux-res", status="resolved"),
                 g.question("qux-part", status="partially_resolved"),
                 g.question("qux-ret", status="retracted"),
                 g.question("qux-old", current=0)),
        lambda: (g.conjecture("cjx-a", 0.5, claim="mid conjecture"),
                 g.conjecture("cjx-b", 0.3, claim="low conjecture B"),
                 g.conjecture("cjx-c", 0.3, claim="low conjecture A"),
                 g.conjecture("cjx-d", 0.55, claim="a fourth conjecture, cut off")),
        lambda: (g.conjecture("cjx-ref", 0.1, status="refuted"),
                 g.conjecture("cjx-sup", 0.1, status="supported"),
                 g.conjecture("cjx-ret", 0.1, status="retracted"),
                 g.conjecture("cjx-old", 0.1, current=0)),
        lambda: (g.node("pnd-1", conf=0.9, claim="pending one"),
                 g.node("pnd-2", conf=0.9, claim="pending two"),
                 g.node("pnd-3", conf=0.9, claim="pending three"),
                 g.node("pnd-done", conf=0.9, claim="settled"),
                 g.marker("pnd-1"), g.marker("pnd-2"), g.marker("pnd-3"),
                 g.marker("pnd-done", status="resolved")),
        lambda: (g.node("pnd-ret", conf=0.9, status="retracted", claim="retracted pending"),
                 g.node("pnd-old", conf=0.9, current=0, claim="superseded pending"),
                 g.marker("pnd-ret"), g.marker("pnd-old")),
    ]
    for i in (order if order is not None else range(len(steps))):
        steps[i]()
    g.commit()


@pytest.fixture(autouse=True)
def _restore_imports():
    _template_db()  # the one-time heavy import happens before the snapshot, so it is not unwound
    mods, path = dict(sys.modules), list(sys.path)
    yield
    for name in [n for n in sys.modules if n not in mods]:
        del sys.modules[name]
    for name, mod in mods.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
    sys.path[:] = path


def _load_tool(home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("study_queue_t", str(_TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ID_PREFIXES = mod._ID_PREFIXES | {"qux"}  # the fixtures' node-id prefix (the tool knows only the store's real ones)
    return mod


def _set_flag(home, value=True, key=FLAG_KEY):
    (Path(home) / "config.json").write_text(json.dumps({"engramd": {key: value}}), encoding="utf-8")


def _make_home(graph):
    """A short path: the owner socket lives at <home>/run/engramd.sock (AF_UNIX path cap)."""
    graph.conn.close()
    home = Path(tempfile.mkdtemp(prefix="sq-"))
    shutil.copyfile(graph.path, home / "knowledge.db")
    return home


@pytest.fixture
def seat(tmp_path):
    graph = Graph(tmp_path)
    _rich_graph(graph)
    home = _make_home(graph)
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def empty_seat(tmp_path):
    graph = Graph(tmp_path)
    graph.commit()
    home = _make_home(graph)
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _dream(home, name, text):
    d = Path(home) / "history" / "dream"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


REPORT = """# Dream report 2026-10-17

## Fairy contribution
nothing relevant

## For the awake seat
1. Check the first thing (qux-101) at the source.
2. Look at the second thing, which also mentions qux-102.
3. Third thing has no node id at all.
4. Fourth thing, already filed.

## Open for tomorrow
- qux-201 (one), qux-202 (two), qux-203 (three): all need external evidence.
- a bullet with no id.

## Currently focused (post-rotation)
nothing

## Sleep-end triage (parent)
1. do now — ran it.
2. defer - nothing new supplied.
4. file (existing #12) – tracked there.
o1. defer — needs outside evidence.
o2. do now - done.
"""


def _queue(mod, home, n=3, **kw):
    return mod.build_queue(Path(home), Path(home) / "knowledge.db", n=n, now=NOW, **kw)


def _refs(q):
    return [t["ref"] for t in q["targets"]]


# ==========================================================================
# Dream-report parsing
# ==========================================================================

def test_d1_both_sections_parsed_and_ids_kept_in_the_ref(empty_seat, monkeypatch):
    """Mutation: drop the open-for-tomorrow section -> the qux-201.. candidate is missing."""
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md", REPORT)
    q = _queue(mod, empty_seat, n=50)
    refs = _refs(q)
    # item 1 is handled (do now), 2 deferred (kept), 3 has no triage line (kept), 4 filed (skipped)
    assert not any("qux-101" in r for r in refs)
    assert any("qux-102" in r for r in refs)
    # one open-for-tomorrow bullet naming three ids stays ONE candidate keeping all three ids
    multi = [r for r in refs if "qux-201" in r]
    assert len(multi) == 1 and "qux-202" in multi[0] and "qux-203" in multi[0]
    assert all(t["source"] == "dream" for t in q["targets"])
    assert all(t["why"].startswith("dream report 2026-10-17") for t in q["targets"])


def test_d2_handled_items_skipped_and_deferred_or_untriaged_kept(empty_seat, monkeypatch):
    """Mutation: remove the handled-item skip -> the do-now / file items appear."""
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md", REPORT)
    q = _queue(mod, empty_seat, n=50)
    whys = " | ".join(t["why"] for t in q["targets"])
    assert "Check the first thing" not in whys          # 1. do now
    assert "Fourth thing" not in whys                    # 4. file (existing #12)
    assert "second thing" in whys                        # 2. defer
    assert "Third thing" in whys                         # 3. no triage line


def test_d2_open_bullets_are_matched_positionally(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md", REPORT)
    whys = [t["why"] for t in _queue(mod, empty_seat, n=50)["targets"]]
    assert any("qux-201" in w for w in whys)             # o1 defer: kept
    assert not any("a bullet with no id" in w for w in whys)   # o2 do now: skipped


def test_d3_missing_triage_section_keeps_every_item(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    text = REPORT.split("## Sleep-end triage")[0]
    _dream(empty_seat, "2026-10-17.md", text)
    q = _queue(mod, empty_seat, n=50)
    assert len(q["targets"]) == 6   # four numbered + two bullets


def test_d4_headings_with_a_parenthetical_suffix_are_matched_by_prefix(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    text = (REPORT.replace("## For the awake seat", "## For the awake seat (2 items)")
                  .replace("## Open for tomorrow", "## Open for tomorrow (carried)")
                  .replace("## Sleep-end triage (parent)", "## Sleep-end triage (parent) (done)"))
    _dream(empty_seat, "2026-10-17.md", text)
    refs = _refs(_queue(mod, empty_seat, n=50))
    assert not any("qux-101" in r for r in refs)         # the triage section was still read
    assert any("qux-102" in r for r in refs) and any("qux-201" in r for r in refs)


def test_d5_dashes_after_the_disposition_em_en_or_hyphen(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    text = (
        "## For the awake seat\n1. one qux-301\n2. two qux-302\n3. three qux-303\n4. four qux-304\n\n"
        "## Sleep-end triage (parent)\n1. do now — x\n2. do now – x\n3. file - x\n4. defer — x\n"
    )
    _dream(empty_seat, "2026-10-17.md", text)
    assert _refs(_queue(mod, empty_seat, n=50)) == ["qux-304"]


def test_d6_no_dream_dir_empty_dir_and_empty_file_give_no_dream_candidates(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    assert _queue(mod, empty_seat)["targets"] == []              # no history/dream at all
    (Path(empty_seat) / "history" / "dream").mkdir(parents=True)
    assert _queue(mod, empty_seat)["targets"] == []              # empty dir
    _dream(empty_seat, "2026-10-17.md", "")
    assert _queue(mod, empty_seat)["targets"] == []              # empty file
    _dream(empty_seat, "2026-10-18.md", "## For the awake seat\n\n## Open for tomorrow\n")
    assert _queue(mod, empty_seat)["targets"] == []              # sections with nothing in them


def test_d7_the_lexicographically_last_file_is_the_one_read(empty_seat, monkeypatch):
    """Mutation: pick the FIRST file instead of the last."""
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-16.md", "## For the awake seat\n1. old thing qux-401\n")
    _dream(empty_seat, "2026-10-18.md", "## For the awake seat\n1. new thing qux-402\n")
    _dream(empty_seat, "2026-10-17.md", "## For the awake seat\n1. mid thing qux-403\n")
    (Path(empty_seat) / "history" / "dream" / "notes.txt").write_text("not a report")
    q = _queue(mod, empty_seat)
    assert _refs(q) == ["qux-402"]
    assert q["targets"][0]["why"].startswith("dream report 2026-10-18")


def test_d12_a_second_dream_on_a_day_is_later_and_a_non_date_md_is_never_a_report(empty_seat, monkeypatch):
    """Review of the sibling PR 3953 (colleague): '-' sorts before '.', so plain filename order read
    2026-10-02.md instead of the later 2026-10-02-turn110.md; the same rule lived here."""
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-02.md", "## For the awake seat\n1. morning dream qux-411\n")
    _dream(empty_seat, "2026-10-02-turn110.md", "## For the awake seat\n1. evening dream qux-412\n")
    _dream(empty_seat, "README.md", "## For the awake seat\n1. a readme qux-413\n")
    _dream(empty_seat, "notes.md", "## For the awake seat\n1. notes qux-414\n")
    q = _queue(mod, empty_seat)
    assert _refs(q) == ["qux-412"]
    assert q["targets"][0]["why"].startswith("dream report 2026-10-02-turn110")


def test_d8_a_wrapped_item_is_one_candidate(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md",
           "## For the awake seat\n1. first line of an item\n   continued here qux-501\n2. next\n")
    q = _queue(mod, empty_seat, n=10)
    assert len(q["targets"]) == 2
    assert "continued here" in q["targets"][0]["why"] and q["targets"][0]["ref"] == "qux-501"


def test_d9_an_item_without_ids_gets_a_file_reference(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md", "## For the awake seat\n1. no id here\n")
    ref = _queue(mod, empty_seat)["targets"][0]["ref"]
    assert "2026-10-17.md" in ref and ref.endswith("#1")


# ==========================================================================
# Priority, N, de-duplication
# ==========================================================================

def test_p1_sources_come_in_priority_order(seat, monkeypatch):
    """Mutation: sort the dream items after the other sources."""
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", "## For the awake seat\n1. dream item qux-601\n")
    q = _queue(mod, seat, n=50)
    assert [t["source"] for t in q["targets"]] == (
        ["dream", "weakest-belief"] + ["question"] * 3 + ["conjecture"] * 3 + ["cascade"] * 2)
    assert [t["rank"] for t in q["targets"]] == list(range(1, 11))
    assert q["candidates"] == 10


@pytest.mark.parametrize("n,expected", [(1, 1), (3, 3), (10, 10), (50, 10)])
def test_p2_n_limits_the_targets(seat, monkeypatch, n, expected):
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", "## For the awake seat\n1. dream item qux-601\n")
    q = _queue(mod, seat, n=n)
    assert len(q["targets"]) == expected and q["candidates"] == 10


def test_p3_fewer_candidates_than_n_prints_what_exists(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md", "## For the awake seat\n1. only item qux-701\n")
    q = _queue(mod, empty_seat, n=10)
    assert len(q["targets"]) == 1 and q["candidates"] == 1


def test_p4_duplicate_refs_collapse_to_the_earlier_source(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", "## For the awake seat\n1. look at qux-002 again\n")
    q = _queue(mod, seat, n=50)
    assert _refs(q).count("qux-002") == 1
    assert [t["source"] for t in q["targets"] if t["ref"] == "qux-002"] == ["dream"]
    assert q["candidates"] == 10   # the cap counts only NEW targets, so qux-002 does not cost a slot


def test_p5_a_multi_id_dream_bullet_absorbs_the_single_node_candidates_it_names(seat, monkeypatch):
    """Found by running the tool on a real store (not by the fixtures): the dream bullet
    `qu_a, qu_b, qu_c` (ref `qu_a,qu_b,qu_c`) was followed by the same questions as separate targets."""
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", "## Open for tomorrow\n- qux-002 and qux-003 both need external evidence\n")
    q = _queue(mod, seat, n=50)
    refs = _refs(q)
    assert refs[0] == "qux-002,qux-003"
    assert "qux-002" not in refs and "qux-003" not in refs
    assert "qux-part" in refs, "a question the bullet does not name stays"
    # the cap counts only NEW targets: the absorbed 002/003 do not use up question slots, so the
    # next-oldest questions (001 after part) take them
    assert refs.count("qux-001") == 1 and [r for r in refs if r.startswith("qux-")] == ["qux-002,qux-003", "qux-part", "qux-001", "qux-004"]
    assert q["candidates"] == 10


def test_p5_control_a_bullet_naming_only_some_ids_of_a_later_candidate_does_not_absorb_it(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", "## Open for tomorrow\n- qux-002 needs evidence\n")
    refs = _refs(_queue(mod, seat, n=50))
    assert refs.count("qux-002") == 1 and "qux-003" in refs


def test_p6_false_ids_in_paths_urls_and_unknown_prefixes_do_not_absorb_real_candidates(seat, monkeypatch):
    """Review of PR 3950: `/tmp/qux-002/x`, a URL fragment and an unknown prefix matched the open id
    pattern and their ids then absorbed the real candidate through the id-level de-dup."""
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", "## Open for tomorrow\n- see /tmp/qux-002/x and https://x.io/pr-1234 and zzz_9999 in prose\n")
    refs = _refs(_queue(mod, seat, n=50))
    assert refs[0].startswith("history/dream/"), "no real id in the bullet: a file ref"
    assert "qux-002" in refs, "the real question qux-002 is still its own candidate"


def test_p7_ids_before_sentence_punctuation_and_with_five_digits_are_ids(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    ids = mod._node_ids("check qu_0020, then ob_1234. also cj_10000; and (ls_0051) but not ob_12")
    assert ids == ["qu_0020", "ob_1234", "cj_10000", "ls_0051"]


def test_p8_only_the_stores_real_prefixes_are_ids(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    mod._ID_PREFIXES = mod._ID_PREFIXES - {"qux"}
    assert mod._node_ids("qux-001 and zz_0001 and ob_0001") == ["ob_0001"]
    assert set("ax cj cs ct df dv ev fl gl ls ob pn qu tk ts".split()) <= set(mod._ID_PREFIXES)


def test_d10_a_fenced_block_is_quoted_text_not_structure(empty_seat, monkeypatch):
    """Review of PR 3950: a `## Sleep-end triage` line inside a code fence was read as the real
    heading and swallowed the items that followed."""
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md",
           "## For the awake seat\n1. real item qux-701\n```\n## Sleep-end triage\n1. do now - quoted\n```\n"
           "2. second item qux-702\n")
    assert _refs(_queue(mod, empty_seat, n=10)) == ["qux-701", "qux-702"]


def test_d11_a_duplicated_heading_first_one_wins(empty_seat, monkeypatch):
    mod = _load_tool(empty_seat, monkeypatch)
    _dream(empty_seat, "2026-10-17.md",
           "## For the awake seat\n1. first qux-711\n## Open for tomorrow\n- bullet qux-712\n"
           "## For the awake seat\n1. a repeated heading qux-713\n")
    refs = _refs(_queue(mod, empty_seat, n=10))
    assert "qux-711" in refs and "qux-712" in refs and "qux-713" not in refs


# ==========================================================================
# Each store source's filter and order
# ==========================================================================

def _by_source(mod, home, source):
    return [t["ref"] for t in _queue(mod, home, n=50)["targets"] if t["source"] == source]


def test_s1_weakest_belief_is_the_top_ranked_entry(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    assert _by_source(mod, seat, "weakest-belief") == ["weak"]


def test_s2_questions_oldest_recalled_first_ties_by_id_and_capped_at_three(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    # qux-part is `partially_resolved` (evidence landed, not settled): it is a question to study, and
    # the oldest-recalled one; qux-001 (recall_turn 9) falls off the cap of three
    assert _by_source(mod, seat, "question") == ["qux-part", "qux-002", "qux-003"]


def test_s3_resolved_retracted_and_superseded_questions_never_appear_partially_resolved_does(seat, monkeypatch):
    """Mutation: remove the resolved-question filter; (separately) the retracted-node filter."""
    mod = _load_tool(seat, monkeypatch)
    conn = sqlite3.connect(str(seat / "knowledge.db"))
    conn.execute("DELETE FROM nodes WHERE id IN ('qux-001','qux-002','qux-003','qux-004')")
    conn.commit()
    conn.close()
    # resolved, retracted and superseded are gone; partially_resolved stays (review of PR 3950)
    assert _by_source(mod, seat, "question") == ["qux-part"]


def test_s4_conjectures_lowest_confidence_first_ties_by_id_capped_at_three(seat, monkeypatch):
    """Mutation: reverse the conjecture order."""
    mod = _load_tool(seat, monkeypatch)
    assert _by_source(mod, seat, "conjecture") == ["cjx-b", "cjx-c", "cjx-a"]


def test_s5_resolved_refuted_retracted_and_superseded_conjectures_never_appear(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    conn = sqlite3.connect(str(seat / "knowledge.db"))
    conn.execute("DELETE FROM nodes WHERE id IN ('cjx-a','cjx-b','cjx-c','cjx-d')")
    conn.commit()
    conn.close()
    assert _by_source(mod, seat, "conjecture") == []


def test_s6_cascade_pending_markers_only_live_nodes_oldest_first_capped_at_two(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    assert _by_source(mod, seat, "cascade") == ["pnd-1", "pnd-2"]
    conn = sqlite3.connect(str(seat / "knowledge.db"))
    conn.execute("UPDATE cascade_markers SET status = 'resolved' WHERE node_id IN ('pnd-1', 'pnd-2')")
    conn.commit()
    conn.close()
    got = _by_source(mod, seat, "cascade")
    assert got == ["pnd-3"] and "pnd-done" not in got and "pnd-ret" not in got


def test_s7_a_retracted_or_noncurrent_node_never_appears_in_any_source(seat, monkeypatch):
    """The cascade source has no status check of its own, so only the live-node filter (current,
    not retracted) keeps pnd-ret and pnd-old out.  Mutation: remove the retracted-node filter."""
    mod = _load_tool(seat, monkeypatch)
    got = {t["ref"] for t in _queue(mod, seat, n=50)["targets"]}
    assert got.isdisjoint({"qux-old", "cjx-old", "qux-ret", "cjx-ret", "pnd-ret", "pnd-old"})
    conn = sqlite3.connect(str(seat / "knowledge.db"))
    conn.execute("UPDATE cascade_markers SET status = 'resolved' WHERE node_id IN ('pnd-1', 'pnd-2', 'pnd-3')")
    conn.commit()
    conn.close()
    assert _by_source(mod, seat, "cascade") == []


# ==========================================================================
# Determinism
# ==========================================================================

def test_x1_same_store_and_files_give_byte_identical_output(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", REPORT)
    a = (mod.format_text(_queue(mod, seat, n=50)), mod.to_json(_queue(mod, seat, n=50)))
    b = (mod.format_text(_queue(mod, seat, n=50)), mod.to_json(_queue(mod, seat, n=50)))
    assert a == b
    keys = list(json.loads(a[1]).keys())
    assert keys == ["generated_at", "targets", "candidates", "not_wired"]
    assert list(json.loads(a[1])["targets"][0].keys()) == ["rank", "source", "ref", "why", "method"]


def test_x2_shuffled_insertion_order_gives_the_same_output(tmp_path, monkeypatch):
    outs = []
    for k, order in enumerate([None, [6, 5, 4, 3, 2, 1, 0], [3, 0, 6, 1, 5, 2, 4]]):
        graph = Graph(tmp_path / f"g{k}") if (tmp_path / f"g{k}").mkdir() is None else None
        _rich_graph(graph, order=order)
        home = _make_home(graph)
        try:
            mod = _load_tool(home, monkeypatch)
            outs.append(mod.format_text(_queue(mod, home, n=50)))
        finally:
            shutil.rmtree(home, ignore_errors=True)
    # cascade order is by marker id (insertion order of the markers within the step), so the
    # shuffles keep the markers' relative order: the three outputs must be identical.
    assert outs[0] == outs[1] == outs[2]


# ==========================================================================
# The empty case and the output contract
# ==========================================================================

def test_e1_nothing_queued_prints_the_line_and_exits_zero(empty_seat):
    r = _cli(empty_seat, ["--now", NOW])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.startswith("study queue: nothing queued (")
    assert r.stdout.count("\n") == 1


def test_e2_empty_json_is_still_an_object_with_no_targets(empty_seat):
    r = _cli(empty_seat, ["--json", "--now", NOW])
    assert r.returncode == 0
    obj = json.loads(r.stdout)
    assert obj["targets"] == [] and obj["candidates"] == 0


def test_o1_text_contract(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", REPORT)
    q = _queue(mod, seat, n=50)
    text = mod.format_text(q)
    lines = text.splitlines()
    assert lines[0] == f"study queue ({len(q['targets'])} of {q['candidates']} candidates) - {NOW}"
    assert lines[1].startswith("Step 0 (do this yourself, not automated): call whats_new")
    assert lines[-1] == "sources not wired in this version: goal tension, syllabus/paper queue, unrun measurements"
    body = lines[2:-1]
    assert len(body) == 2 * len(q["targets"])
    for i, t in enumerate(q["targets"]):
        head, method = body[2 * i], body[2 * i + 1]
        assert head.startswith(f"{i + 1}. [{t['source']}] {t['ref']} - ")
        assert method == f"   method: {mod.METHODS[t['source']]}"
        assert 0 < len(t["why"]) <= 160 and "\n" not in t["why"]
        assert t["method"] == mod.METHODS[t["source"]]


def test_o2_method_table_is_the_specified_strings(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    assert mod.METHODS == {
        "dream": "do the item as written, then write one ENGRAM observation of the result (what you ran, what it returned)",
        "weakest-belief": "re-verify its weakest premise at the source, then supersede, retract or confirm it with an observation",
        "question": "search the graph, then one external source; write an observation that answers or narrows it, then engram_resolve or leave it open with what you learned",
        "conjecture": "design one check that could falsify it, run it, then resolve, promote or refute with the result as evidence",
        "cascade": "run the engram-resolve-cascade skill on this node",
    }


def test_o3_a_long_claim_is_clipped_to_160(empty_seat, monkeypatch, tmp_path):
    graph = Graph(tmp_path)
    graph.question("qux-long", claim="word " * 200)
    graph.commit()
    home = _make_home(graph)
    try:
        mod = _load_tool(home, monkeypatch)
        why = _queue(mod, home)["targets"][0]["why"]
        assert len(why) <= 160
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ==========================================================================
# Read-only
# ==========================================================================

def test_r1_the_whole_tool_runs_on_a_query_only_connection(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", REPORT)
    real, seen = sqlite3.connect, []

    def query_only_connect(*a, **k):
        conn = real(*a, **k)
        conn.execute("PRAGMA query_only=1")
        seen.append(a[0] if a else "")
        return conn

    monkeypatch.setattr(sqlite3, "connect", query_only_connect)
    q = _queue(mod, seat, n=50)
    assert seen and len(q["targets"]) >= 10


def _listing(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        st = p.stat()
        out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns,
                                         hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "")
    return out


def test_r2_the_tool_writes_nothing_under_the_home(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    _dream(seat, "2026-10-17.md", REPORT)
    _set_flag(seat, False)
    before = _listing(seat)
    _queue(mod, seat, n=50)
    r = _cli(seat, ["--n", "10", "--now", NOW])
    assert r.returncode == 0, r.stderr
    assert _listing(seat) == before


# ==========================================================================
# Owner-read: registration, differential, refusal semantics
# ==========================================================================

class Opens:
    """Raise-and-record every way THIS thread could open a sqlite store."""

    def __init__(self):
        self.record = []
        self._thread = threading.get_ident()

    def install(self, monkeypatch):
        real = {"connect": sqlite3.connect, "dbapi2": sqlite3.dbapi2.connect}

        def guard(name):
            def f(*a, **k):
                if threading.get_ident() != self._thread:
                    return real[name](*a, **k)
                self.record.append((name, str(a[0]) if a else ""))
                raise AssertionError(f"direct open via {name}: {a[:1]}")
            return f

        monkeypatch.setattr(sqlite3, "connect", guard("connect"))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", guard("dbapi2"))
        return self


def _cli(home, args, **extra_env):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env.update(extra_env)
    return subprocess.run([sys.executable, str(_TOOL_PATH), *args], capture_output=True,
                          text=True, env=env, timeout=120)


def _sites():
    data = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    return [(t["sql"], s) for t in data["templates"] for s in t["sites"] if s.startswith(SITE_PREFIX)]


def test_t1_the_tool_has_registered_templates_under_its_own_sites():
    sites = _sites()
    assert sites, "no template is registered under tools/study_queue.py"
    for sql, _site in sites:
        assert sql.startswith("SELECT")


def test_t2_the_scan_config_names_the_tool_as_a_root():
    cfg = json.loads((REPO_ROOT / "tools" / "engramd_read_scan.config.json").read_text())
    assert any(r.startswith(SITE_PREFIX) for r in cfg["roots"]), "no scanner root for the tool"


def test_t3_every_statement_a_flag_on_run_sends_is_a_registered_template(seat, monkeypatch):
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    mine = {sql for sql, _s in _sites()}
    with FakeOwner(seat) as owner:
        q = mod.build_queue(seat, seat / "knowledge.db", n=50, now=NOW)
    sent = [r["params"]["sql"] for r in owner.requests if r["method"] == "read"]
    assert q["targets"] and sent
    assert set(sent) <= mine, set(sent) - mine
    assert set(sent) <= registered_sql()


def test_t4_registered_templates_survive_registry_classification(seat):
    conn = sqlite3.connect(str(seat / "knowledge.db"))
    try:
        reg = rr.load_registry(REPO_ROOT / "src" / "engram" / "engramd_read_templates.json", conn)
        mine = _sites()
        assert mine
        for sql, _site in mine:
            assert sql not in reg.refused, reg.refused.get(sql)
            assert hashlib.sha256(sql.encode("utf-8")).hexdigest() in reg.templates
    finally:
        conn.close()


def test_c1_flag_off_and_flag_on_with_a_fake_owner_give_the_same_queue(seat, monkeypatch):
    _dream(seat, "2026-10-17.md", REPORT)
    _set_flag(seat, False)
    direct = _queue(_load_tool(seat, monkeypatch), seat, n=50)
    _set_flag(seat, True)
    mod_on = _load_tool(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        via_owner = _queue(mod_on, seat, n=50)
    assert via_owner == direct and len(direct["targets"]) >= 10
    assert any(r["method"] == "read" for r in owner.requests)


def test_c2_flag_off_never_contacts_the_owner(seat, monkeypatch):
    _set_flag(seat, False)
    mod = _load_tool(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        assert _queue(mod, seat)["targets"]
    assert owner.requests == []


def test_c3_flag_on_owner_down_raises_never_an_empty_queue_and_never_opens_the_store(seat, monkeypatch):
    """Mutation: the owner-down path silently returns an empty queue (or reads directly)."""
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with pytest.raises(mod.StudyQueueOwnerReadError) as exc:
        _queue(mod, seat)
    assert "owner" in str(exc.value).lower()
    assert "empty" in str(exc.value).lower()
    assert opens.record == [], opens.record


@pytest.mark.parametrize("answer", [
    {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "unregistered_template", "detail": "d", "remedy": "r"},
    {"status": "ok", "result": "not-a-list"},
])
def test_c4_every_other_owner_failure_raises_too(seat, monkeypatch, answer):
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    owner = FakeOwner(seat)
    owner._answer = lambda request, a=answer: (
        {"status": "ok", "result": "pong"} if request.get("method") == "ping" else a)
    with owner:
        with pytest.raises(mod.StudyQueueOwnerReadError):
            _queue(mod, seat)


class _BadRowsOwner(FakeOwner):
    def __init__(self, home, marker, rows):
        super().__init__(home)
        self._marker, self._rows = marker, rows

    def _answer(self, request):
        sql = (request.get("params") or {}).get("sql") or ""
        if request.get("method") == "read" and self._marker in sql:
            return {"status": "ok", "result": self._rows}
        return super()._answer(request)


@pytest.mark.parametrize("marker,rows", [
    ("FROM nodes WHERE is_current = 1 AND COALESCE(status, '') != 'retracted' AND type IN", [["n"]]),
    ("FROM nodes WHERE is_current = 1 AND COALESCE(status, '') != 'retracted' AND type IN",
     [[None, None, None, None, None, None]]),
    ("FROM cascade_markers", [["a", "b"]]),
    ("FROM cascade_markers", [[1]]),
])
def test_c5_malformed_owner_rows_raise_never_a_shorter_queue(seat, monkeypatch, marker, rows):
    _set_flag(seat)
    mod = _load_tool(seat, monkeypatch)
    with _BadRowsOwner(seat, marker, rows):
        with pytest.raises(mod.StudyQueueOwnerReadError):
            _queue(mod, seat, n=50)


def test_c6_the_spec_names_the_tools_own_flag_and_error_class(seat, monkeypatch):
    mod = _load_tool(seat, monkeypatch)
    assert issubclass(mod.StudyQueueOwnerReadError, RuntimeError)
    assert mod.OWNER_FLAG_KEY == FLAG_KEY
    spec = mod._owner_spec()
    assert spec.flag_key == FLAG_KEY and spec.error_cls is mod.StudyQueueOwnerReadError


def test_c7_the_module_has_no_connect_call_of_its_own():
    src = _TOOL_PATH.read_text()
    assert "sqlite3.connect(" not in src and ".connect(" not in src


def test_c8_the_weakest_belief_read_is_governed_by_its_own_flag(seat, monkeypatch):
    """The weakest-belief read goes through weakest_load_bearing.read_ranking: ITS flag ON with
    no owner up raises ITS error even when this tool's flag is OFF (no silent skip)."""
    _set_flag(seat, True, key="weakest_load_bearing_reads_via_owner")
    mod = _load_tool(seat, monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        _queue(mod, seat)
    assert type(exc.value).__name__ == "WeakestLoadBearingOwnerReadError"


# ---- the CLI --------------------------------------------------------------------------------

def test_l1_cli_owner_down_exits_one_with_nothing_on_stdout(seat):
    """Mutation: an empty queue on owner-down would exit 0 and print the nothing-queued line."""
    _set_flag(seat)
    r = _cli(seat, ["--json", "--now", NOW])
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert r.stdout == ""
    # (engram_core's own sqlite-vec notice may precede it on a host without sqlite-vec)
    assert r.stderr.splitlines()[-1].startswith("StudyQueueOwnerReadError: ")
    assert sum(1 for ln in r.stderr.splitlines() if ln.startswith("StudyQueueOwnerReadError")) == 1


def test_l2_cli_owner_up_matches_flag_off(seat):
    _dream(seat, "2026-10-17.md", REPORT)
    _set_flag(seat, False)
    off = _cli(seat, ["--n", "10", "--now", NOW])
    _set_flag(seat, True)
    with FakeOwner(seat):
        on = _cli(seat, ["--n", "10", "--now", NOW])
    assert off.returncode == 0 and on.returncode == 0, (off.stderr, on.stderr)
    assert on.stdout == off.stdout and "method:" in on.stdout


def test_l3_cli_default_n_is_three_and_text_is_the_default(seat):
    _set_flag(seat, False)
    r = _cli(seat, ["--now", NOW])
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("study queue (3 of ")
    assert len([ln for ln in r.stdout.splitlines() if ln[:1].isdigit()]) == 3


@pytest.mark.parametrize("bad", ["0", "-2", "x"])
def test_l4_cli_rejects_a_bad_n_with_a_usage_error(seat, bad):
    r = _cli(seat, ["--n", bad])
    assert r.returncode == 2 and r.stdout == ""


def test_l5_cli_missing_store_exits_one_with_the_class_on_stderr(tmp_path):
    r = _cli(tmp_path, [])
    assert r.returncode == 1 and r.stdout == ""
    assert r.stderr.splitlines()[-1].startswith("FileNotFoundError: ")


def test_l6_cli_home_flag_overrides_the_environment(seat, tmp_path):
    _dream(seat, "2026-10-17.md", "## For the awake seat\n1. from the flagged home ob-801\n")
    r = _cli(tmp_path, ["--home", str(seat), "--db", str(seat / "knowledge.db"), "--json", "--now", NOW])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["targets"][0]["ref"] == "ob-801"
