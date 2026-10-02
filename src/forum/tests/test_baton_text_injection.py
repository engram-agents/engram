"""Contract tests: no request body can inject a frontmatter line into a baton file.

#3039 U2b round 4 (security fix), written BEFORE the fix. Every forum write-body
interpolates client strings into the baton file's frontmatter / turn log
verbatim (``turn_reason: "{reason}"``, ``title``, the ``github`` anchor, agent
names, ...), only ``.strip()``-ed, so an INTERIOR line break survives.
``parse_frontmatter`` lets the LAST duplicate key win, so a client could write
``phase: merge`` with NO approval anywhere (``reason = 'x"\\nphase: merge\\nz: "'``).

The contract these tests pin:

  R1. ONE shared sanitiser, applied before any lock / seq / write on every route
      that writes a client string, plus a last-resort assertion in the write
      layer (``update_frontmatter`` / ``append_turn_log`` / ``init``).
        * FREE-TEXT fields (reason, turn_reason, title, new_title): every run of
          line-break / control characters becomes ONE space; stored on one line.
        * IDENTIFIER fields (github, agent, to_agent, colleague, author,
          participant(s), pool_sentinel, project id, status values, phase,
          transition_phase): any such character => 422 naming the route and the
          field, nothing written, no seq consumed.
  R2. A baton file with a duplicated guarded frontmatter key is refused with a
      422 naming the key (``_advance_gate``, ``_check_flip_gate``, ...).
  R5. A non-string value where a string is expected is a 422 naming the route and
      field -- never a 500, never a write. Absent / null stays "absent".

Identities are SYNTHETIC; no real fleet name appears here.
"""

import inspect
import re
from pathlib import Path

import pytest

from forum import server as server_mod
from forum.coordination import markdown as md
from forum.coordination import (
    advance as _advance_wb,
    flip as _flip_wb,
    init as _init,
)
from forum.coordination.markdown import parse_frontmatter
from forum.tests.test_baton_advance_api import (
    ANCHOR,
    AUTHOR,
    COLLEAGUE,
    OTHER,
    REPO,
    SEAT,
    SENTINEL,
    World,
    rv,
)

NEWBIE = "example-agent-new"

# Every separator the sanitiser must neutralise. Measured: only LF and CR (and
# CRLF) actually split frontmatter lines through the store's text-mode read; the
# rest are inert today but are rejected / normalised anyway so a parser change
# cannot widen the splitter silently.
SEPS = {
    "LF": "\n",
    "CR": "\r",
    "CRLF": "\r\n",
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "\x85",
    "LS": " ",
    "PS": " ",
    "NUL": "\x00",
    "DEL": "\x7f",
}
BAD_CHARS = set("".join(SEPS.values()))
# LF is also the file's own line terminator, so its absence is asserted through the
# stored-text / key / phase checks instead.
UNSAFE_CHARS = BAD_CHARS - {"\n"}
INERT = "\x0b\x0c\x1c\x85  "  # measured inert (A4)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.gh.reviews = []  # NO approval anywhere
    return w


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def disk(world, pid):
    """The file's bytes as decoded text WITHOUT newline translation."""
    for d in ("projects", "archive"):
        p = Path(world.store.root) / d / f"{pid}.md"
        if p.exists():
            return p.read_bytes().decode("utf-8")
    return None


def seq_state(world):
    return (world.allocator.current(), world.allocator._seq)


def universal(raw):
    """What the store's text-mode read hands the parser."""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def fm_keys(raw):
    text = universal(raw)
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return []
    return [k.lower() for k in re.findall(r"^(\w[\w-]*):", m.group(1), re.M)]


def phase_of_raw(raw):
    if raw is None:
        return None
    return (parse_frontmatter(universal(raw))[0].get("phase") or "").strip() or None


def phase_first(world, pid):
    """Re-order the file so `phase:` sits right after `project:` -- the layout in
    which any LATER injected key wins (the worst case for last-wins parsing)."""
    p = Path(world.store.projects_dir) / f"{pid}.md"
    lines = p.read_text().split("\n")
    idx = [i for i, ln in enumerate(lines) if ln.startswith("phase:")]
    if idx:
        ph = lines.pop(idx[0])
        at = next(i for i, ln in enumerate(lines) if ln.startswith("project:")) + 1
        lines.insert(at, ph)
        p.write_text("\n".join(lines))


def post_safe(world, url, body):
    """POST; an exception out of the handler (a 500 in production) is reported
    as status 599 so the failure is an assertion, not an error."""
    try:
        resp = world.client.post(url, json=body)
    except Exception as exc:  # noqa: BLE001
        text = f"EXCEPTION {type(exc).__name__}: {exc}"

        class _R:
            status_code = 599

            def get_json(self, *a, **k):
                return {"error": text}
        return _R()
    return resp


# seeds: name -> builder(world, n) -> pid.  Unique pid per call so one World can
# run every separator.
def _pr(world, n, phase, turn):
    pid = f"PR-{9000 + n}"
    world.seed(pid, phase=phase, turn=turn, github=f"pr/{REPO}/{9000 + n}")
    phase_first(world, pid)
    return pid


def _bunch(world, n, phase, turn):
    pid = f"pool-b{n}"
    _init(world.store, world.allocator, pid, title="bunch", status="in-progress",
          turn=turn, participants=[AUTHOR, COLLEAGUE, OTHER], turn_reason="start",
          github="project/7", author=AUTHOR, transition_phase=phase)
    phase_first(world, pid)
    return pid


SEEDS = {
    "peer": lambda w, n: _pr(w, n, "peer", COLLEAGUE),
    "fairy": lambda w, n: _pr(w, n, "fairy", AUTHOR),
    "legacy": lambda w, n: _pr(w, n, None, COLLEAGUE),
    "merge": lambda w, n: _pr(w, n, "merge", COLLEAGUE),
    "pool": lambda w, n: _bunch(w, n, "pool", SENTINEL),
    "claimed": lambda w, n: _bunch(w, n, "claimed", OTHER),
}

# route key -> (url suffix, base body).  Every base body is a valid request.
BASES = {
    "flip-colleague": ("flip", {"agent": AUTHOR, "to_agent": COLLEAGUE, "reason": "please review"}),
    "flip-seat": ("flip", {"agent": AUTHOR, "to_agent": SEAT, "reason": "ready"}),
    "advance": ("advance", {"agent": COLLEAGUE, "to_agent": SEAT, "reason": "ok"}),
    "claim": ("claim", {"agent": OTHER, "pool_sentinel": SENTINEL}),
    "release": ("release", {"agent": OTHER, "pool_sentinel": SENTINEL, "reason": "back"}),
    "close": ("status", {"agent": AUTHOR, "new_status": "cancelled", "reason": "drop"}),
    "reopen": ("status", {"agent": AUTHOR, "new_status": "in-progress", "reason": "again"}),
    "gc": ("gc", {"agent": AUTHOR, "new_status": "merged", "reason": "gc"}),
    "merge": ("merge", {"agent": AUTHOR}),
    "rename": ("rename", {"agent": AUTHOR, "new_title": "renamed"}),
    "anchor": ("anchor", {"agent": AUTHOR, "github": ANCHOR}),
    "participants": ("participants", {"agent": AUTHOR, "participant": NEWBIE}),
    "colleague": ("colleague", {"agent": AUTHOR, "colleague": OTHER}),
}
_PH = {"phase": "id", "transition_phase": "id"}
# field -> kind: "id" (identifier: reject) | "text" (free text: normalise)
FIELD_KINDS = {
    "flip-colleague": {"agent": "id", "to_agent": "id", "reason": "text", **_PH},
    "flip-seat": {"agent": "id", "to_agent": "id", "reason": "text", **_PH},
    "advance": {"agent": "id", "to_agent": "id", "reason": "text"},
    "claim": {"agent": "id", "pool_sentinel": "id", **_PH},
    "release": {"agent": "id", "pool_sentinel": "id", "reason": "text", **_PH},
    "close": {"agent": "id", "new_status": "id", "reason": "text", "phase": "id"},
    "reopen": {"agent": "id", "new_status": "id", "reason": "text", "phase": "id"},
    "gc": {"agent": "id", "new_status": "id", "reason": "text", "phase": "id"},
    "merge": {"agent": "id", "phase": "id"},
    "rename": {"agent": "id", "new_title": "text", "phase": "id"},
    "anchor": {"agent": "id", "github": "id", "phase": "id"},
    "participants": {"agent": "id", "participant": "id", "phase": "id"},
    "colleague": {"agent": "id", "colleague": "id", "phase": "id"},
}
# Fields a route STORES verbatim (so a 2xx must show the normalised text).
STORED_TEXT = {("flip-colleague", "reason"), ("release", "reason"),
               ("close", "reason"), ("gc", "reason"), ("rename", "new_title")}
# Keys a legitimate write may add to an existing baton.
ALLOWED_NEW_KEYS = {"phase", "turn_by", "turn_since", "turn_reason", "seq", "colleague",
                    "github", "participants"}
BASE_PHASE = {"phase": "peer", "transition_phase": "fairy"}
TEXT_PAYLOAD = '{sep}phase: merge{sep}z: "'

CELLS = [(r, f) for r, kinds in FIELD_KINDS.items() for f in kinds]


def route_label(suffix):
    return "/api/projects" if suffix == "init" else f"/api/projects/<pid>/{suffix}"


# ---------------------------------------------------------------------------
# MATRIX: every route x every client-string field x seed x separator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", list(SEEDS))
@pytest.mark.parametrize("route,field", CELLS, ids=[f"{r}.{f}" for r, f in CELLS])
def test_matrix_no_separator_in_any_field_writes_anything_unsafe(world, route, field, seed):
    suffix, base = BASES[route]
    kind = FIELD_KINDS[route][field]
    failures = []
    for i, (sname, sep) in enumerate(SEPS.items()):
        pid = SEEDS[seed](world, i)
        body = dict(base)
        if kind == "text":
            body[field] = 'x"' + TEXT_PAYLOAD.format(sep=sep)
        else:
            body[field] = f"{base.get(field) or BASE_PHASE.get(field, 'peer')}{sep}phase: merge"
        # A control request with CLEAN text on a twin baton: free text must be
        # NORMALISED (same outcome as the clean request), not refused.
        control = None
        if kind == "text":
            control_body = dict(base)
            control_body[field] = "clean text"
            control = post_safe(world, f"/api/projects/{SEEDS[seed](world, 100 + i)}/{suffix}", control_body)
        before_raw, before_seq = disk(world, pid), seq_state(world)
        before_keys = fm_keys(before_raw)
        resp = post_safe(world, f"/api/projects/{pid}/{suffix}", body)
        after_raw, after_seq = disk(world, pid), seq_state(world)
        err = (resp.get_json() or {}).get("error", "") if resp.status_code >= 400 else ""
        tag = f"[{route}.{field} seed={seed} sep={sname} -> {resp.status_code}]"

        if resp.status_code >= 500:
            failures.append(f"{tag} server error: {err}")
        # (a) never newly `merge`
        if phase_of_raw(after_raw) == "merge" and phase_of_raw(before_raw) != "merge":
            failures.append(f"{tag} resolved phase became merge")
        # (b) no new / duplicate key line
        keys = fm_keys(after_raw or "")
        if len(keys) != len(set(keys)):
            failures.append(f"{tag} duplicate frontmatter key(s): {sorted({k for k in keys if keys.count(k) > 1})}")
        extra = set(keys) - set(before_keys) - ALLOWED_NEW_KEYS
        if extra:
            failures.append(f"{tag} injected frontmatter key(s): {sorted(extra)}")
        # (c) identifiers refused; free text stored on one line
        if kind == "id":
            if resp.status_code != 422:
                failures.append(f"{tag} identifier with {sname} was not refused with 422")
            else:
                low = err.lower()
                if route_label(suffix) not in err or f"'{field}'" not in err or "control" not in low:
                    failures.append(f"{tag} 422 does not name route+field+control chars: {err!r}")
            if after_raw != before_raw or after_seq != before_seq:
                failures.append(f"{tag} refused identifier still wrote the file / consumed a seq")
        else:
            if resp.status_code != control.status_code:
                failures.append(f"{tag} free text was answered {resp.status_code} but the same request "
                                f"with clean text is {control.status_code} (normalise, do not refuse)")
            if after_raw is not None and (UNSAFE_CHARS & set(after_raw)):
                failures.append(f"{tag} file still carries line-break/control chars "
                                f"{sorted(hex(ord(c)) for c in UNSAFE_CHARS & set(after_raw))}")
            if resp.status_code >= 400 and (after_raw != before_raw or after_seq != before_seq):
                failures.append(f"{tag} refused request still wrote / consumed a seq")
            if resp.status_code in (200, 201) and (route, field) in STORED_TEXT:
                want = 'x" phase: merge z: "'
                if want not in universal(after_raw or ""):
                    failures.append(f"{tag} normalised text {want!r} not stored on one line")
    assert not failures, "\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# init (a NEW baton): every field x separator
# ---------------------------------------------------------------------------

INIT_KINDS = {
    "agent": "id", "project_id": "id", "title": "text", "status": "id", "turn": "id",
    "turn_reason": "text", "github": "id", "colleague": "id", "author": "id",
    "participants": "id", "phase": "id", "transition_phase": "id",
}
INIT_ALLOWED_KEYS = {"project", "title", "status", "turn", "turn_since", "turn_reason",
                     "turn_by", "participants", "github", "colleague", "author", "phase", "seq"}


def init_body(pid):
    return {"agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
            "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR, COLLEAGUE],
            # r9: the anchor must name the id's own number (PR-91NN -> pr/<REPO>/91NN)
            "github": f"pr/{REPO}/{pid.rsplit('-', 1)[-1]}", "colleague": COLLEAGUE, "author": AUTHOR}


@pytest.mark.parametrize("field", list(INIT_KINDS))
def test_matrix_init_never_writes_an_unsafe_new_baton(world, field):
    kind = INIT_KINDS[field]
    failures = []
    for i, (sname, sep) in enumerate(SEPS.items()):
        pid = f"PR-91{i:02d}"
        body = init_body(pid)
        if field == "project_id":
            body["project_id"] = f"{pid}{sep}phase: merge"
        elif field == "participants":
            body["participants"] = [AUTHOR, f"{COLLEAGUE}{sep}phase: merge"]
        elif field in ("phase", "transition_phase"):
            body[field] = f"fairy{sep}phase: merge"
        elif kind == "text":
            body[field] = 'x"' + TEXT_PAYLOAD.format(sep=sep)
        else:
            body[field] = f"{body[field]}{sep}phase: merge"
        before_seq = seq_state(world)
        before_files = sorted(p.name for p in Path(world.store.projects_dir).glob("*")) \
            if Path(world.store.projects_dir).exists() else []
        resp = post_safe(world, "/api/projects", body)
        after_files = sorted(p.name for p in Path(world.store.projects_dir).glob("*")) \
            if Path(world.store.projects_dir).exists() else []
        tag = f"[init.{field} sep={sname} -> {resp.status_code}]"
        err = (resp.get_json() or {}).get("error", "") if resp.status_code >= 400 else ""
        if resp.status_code >= 500:
            failures.append(f"{tag} server error: {err}")
        if kind == "id":
            if resp.status_code != 422:
                failures.append(f"{tag} identifier not refused with 422")
            elif route_label("init") not in err or f"'{field}'" not in err or "control" not in err.lower():
                failures.append(f"{tag} 422 does not name route+field+control chars: {err!r}")
            if after_files != before_files or seq_state(world) != before_seq:
                failures.append(f"{tag} refused init still created a file / consumed a seq")
        else:
            if resp.status_code != 201:
                failures.append(f"{tag} free text should be normalised and accepted")
            else:
                raw = disk(world, pid)
                if UNSAFE_CHARS & set(raw):
                    failures.append(f"{tag} new file carries line-break/control chars")
                keys = fm_keys(raw)
                if len(keys) != len(set(keys)) or set(keys) - INIT_ALLOWED_KEYS:
                    failures.append(f"{tag} new file keys {keys}")
                if phase_of_raw(raw) == "merge":
                    failures.append(f"{tag} new baton resolved phase merge")
                if 'x" phase: merge z: "' not in universal(raw):
                    failures.append(f"{tag} normalised text not stored on one line")
    assert not failures, "\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# The reviewer's exact repros
# ---------------------------------------------------------------------------

INJ = 'x"\nphase: merge\nz: "'


class TestMeasuredRepros:
    def test_anchor_injection_on_explicit_fairy_baton(self, world):
        world.seed("PR-9001", phase="fairy", turn=AUTHOR)
        phase_first(world, "PR-9001")
        before = disk(world, "PR-9001")
        resp = world.client.post("/api/projects/PR-9001/anchor", json={
            "agent": AUTHOR, "github": "pr/example-org/example-repo/9001\nphase: merge"})
        assert resp.status_code == 422, resp.get_json()
        assert disk(world, "PR-9001") == before
        assert phase_of_raw(disk(world, "PR-9001")) == "fairy"

    def test_legacy_rename_injection(self, world):
        world.seed("PR-9001", phase=None, turn=COLLEAGUE)
        resp = world.client.post("/api/projects/PR-9001/rename",
                                 json={"agent": AUTHOR, "new_title": INJ})
        assert resp.status_code in (201, 422), resp.get_json()
        raw = disk(world, "PR-9001")
        assert phase_of_raw(raw) != "merge"
        assert len(fm_keys(raw)) == len(set(fm_keys(raw)))
        assert "z" not in fm_keys(raw)

    def test_legacy_anchor_injection(self, world):
        world.seed("PR-9001", phase=None, turn=COLLEAGUE)
        before = disk(world, "PR-9001")
        resp = world.client.post("/api/projects/PR-9001/anchor", json={
            "agent": AUTHOR, "github": INJ})
        assert resp.status_code == 422, resp.get_json()
        assert disk(world, "PR-9001") == before

    def test_legacy_flip_reason_injection(self, world):
        world.seed("PR-9001", phase=None, turn=COLLEAGUE)
        resp = world.client.post("/api/projects/PR-9001/flip", json={
            "agent": COLLEAGUE, "to_agent": AUTHOR, "reason": INJ})
        assert resp.status_code in (201, 422), resp.get_json()
        raw = disk(world, "PR-9001")
        assert phase_of_raw(raw) != "merge", raw
        assert "z" not in fm_keys(raw)
        if resp.status_code == 201:
            assert 'turn_reason: "x" phase: merge z: ""' in universal(raw)


class TestInertBytesA4:
    """Kepler's extra cell: the bytes measured inert today are normalised anyway
    and the parser is pinned so a change cannot widen the splitter silently."""

    @pytest.mark.parametrize("route,field", [(r, f) for r, f in CELLS if FIELD_KINDS[r][f] == "text"])
    def test_inert_bytes_in_free_text_leave_phase_and_keys_unchanged(self, world, route, field):
        suffix, base = BASES[route]
        for seed in ("fairy", "legacy", "peer", "pool", "claimed"):
            pid = SEEDS[seed](world, 40 + list(SEEDS).index(seed))
            control_body = dict(base)
            control_body[field] = "clean text"
            control = post_safe(
                world, f"/api/projects/{SEEDS[seed](world, 60 + list(SEEDS).index(seed))}/{suffix}",
                control_body)
            before_raw, before_seq = disk(world, pid), seq_state(world)
            before_keys, before_phase = fm_keys(before_raw), phase_of_raw(before_raw)
            body = dict(base)
            body[field] = f"a{INERT}phase: merge{INERT}b"
            resp = post_safe(world, f"/api/projects/{pid}/{suffix}", body)
            assert resp.status_code < 500, (route, field, seed, resp.get_json())
            # normalised and accepted exactly like clean text -- not refused
            assert resp.status_code == control.status_code, (route, field, seed, resp.get_json())
            raw = disk(world, pid)
            after_phase = phase_of_raw(raw)
            if before_phase:
                assert after_phase == before_phase, (route, field, seed)
            else:  # a legacy baton may be CONVERTED by the server -- never to `merge`
                assert after_phase != "merge", (route, field, seed)
            assert not (set(INERT) & set(raw)), (route, field, seed, "inert bytes were stored")
            assert set(fm_keys(raw)) - set(before_keys) <= ALLOWED_NEW_KEYS

    @pytest.mark.parametrize("ch", list(INERT), ids=[hex(ord(c)) for c in INERT])
    def test_parse_frontmatter_does_not_split_a_line_on_inert_chars(self, ch):
        text = f"---\nproject: PR-1\ntitle: a{ch}phase: merge\nturn: x\n---\nbody\n"
        fields, _ = parse_frontmatter(text)
        assert "phase" not in fields
        assert fields["title"] == f"a{ch}phase: merge"
        assert fields["turn"] == "x"
        assert len(md.FRONTMATTER_FIELD_RE.findall(text.split("---")[1])) == 3


# ---------------------------------------------------------------------------
# R2 -- duplicate guarded frontmatter keys
# ---------------------------------------------------------------------------

GUARDED = ["phase", "turn", "github", "colleague", "author", "status", "participants"]


def with_dup(world, pid, key, *, value=None, key_text=None):
    """Append a second `key:` line inside the frontmatter, same value."""
    p = Path(world.store.projects_dir) / f"{pid}.md"
    text = p.read_text()
    fields, _ = parse_frontmatter(text)
    val = fields[key] if value is None else value
    head, rest = text.split("\n---\n", 1)
    p.write_text(f"{head}\n{key_text or key}: {val}\n---\n{rest}")


class TestDuplicateKeys:
    def test_unit_frontmatter_duplicate_keys(self):
        clean = "---\nproject: PR-1\nphase: peer\nturn: a\n---\nbody\nphase: x\nphase: y\n"
        assert md.frontmatter_duplicate_keys(clean) == []
        assert md.frontmatter_duplicate_keys("no frontmatter at all") == []
        dup = "---\nproject: PR-1\nphase: peer\nturn: a\nphase: merge\n---\nbody\n"
        assert md.frontmatter_duplicate_keys(dup) == ["phase"]
        mixed = "---\nPhase: peer\nTURN: a\nturn: b\nphase: merge\ntitle: t\n---\n"
        assert sorted(md.frontmatter_duplicate_keys(mixed)) == ["phase", "turn"]

    def test_parse_frontmatter_last_wins_is_unchanged(self):
        dup = "---\nproject: PR-1\nphase: peer\nphase: merge\n---\nbody\n"
        assert parse_frontmatter(dup)[0]["phase"] == "merge"

    @pytest.mark.parametrize("key", GUARDED)
    def test_advance_route_refuses_naming_the_key(self, world, key):
        world.gh.reviews = [rv("R1", "example-gh-colleague", "APPROVED")]  # would pass
        world.seed("PR-9002", github=f"pr/{REPO}/9002")
        with_dup(world, "PR-9002", key)
        before, seq0 = disk(world, "PR-9002"), seq_state(world)
        resp = world.advance("PR-9002")
        assert resp.status_code == 422, resp.get_json()
        err = resp.get_json()["error"]
        assert "duplicate frontmatter key" in err and key in err and "repair" in err
        assert disk(world, "PR-9002") == before and seq_state(world) == seq0

    def test_advance_route_dup_key_is_case_insensitive(self, world):
        world.seed()
        with_dup(world, "PR-9001", "phase", key_text="PHASE")
        resp = world.advance()
        assert resp.status_code == 422 and "phase" in resp.get_json()["error"]

    @pytest.mark.parametrize("key", GUARDED)
    def test_raw_flip_to_the_merge_seat_refuses_naming_the_key(self, world, key):
        world.seed()
        with_dup(world, "PR-9001", key)
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        resp = world.flip(to=SEAT)
        assert resp.status_code == 422, resp.get_json()
        err = resp.get_json()["error"]
        assert "duplicate frontmatter key" in err and key in err
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0

    @pytest.mark.parametrize("key", GUARDED)
    def test_check_flip_gate_itself_refuses_naming_the_key(self, world, key):
        world.seed()
        with_dup(world, "PR-9001", key)
        refusal = server_mod._check_flip_gate(world.store, "PR-9001", SEAT)
        assert refusal is not None
        msg, status = refusal
        assert status == 422 and "duplicate frontmatter key" in msg and key in msg

    @pytest.mark.parametrize("key", GUARDED)
    def test_check_flip_gate_refuses_before_classifying_even_a_review_stage_flip(self, world, key):
        """The flip to the colleague never reaches the strict gate, so only
        `_check_flip_gate`'s OWN duplicate-key check can refuse it (the
        classification it makes -- maintainer or not -- reads turn / colleague /
        author, which a duplicated key can change)."""
        world.seed()
        with_dup(world, "PR-9001", key)
        refusal = server_mod._check_flip_gate(world.store, "PR-9001", COLLEAGUE)
        assert refusal is not None, "a review-stage flip on a duplicate-key baton was classified"
        msg, status = refusal
        assert status == 422 and "duplicate frontmatter key" in msg and key in msg

    @pytest.mark.parametrize("key", GUARDED)
    def test_anchor_route_resolving_the_phase_refuses_naming_the_key(self, world, key):
        world.seed("PR-9001", phase="fairy", turn=AUTHOR)
        with_dup(world, "PR-9001", key)
        before = disk(world, "PR-9001")
        resp = world.client.post("/api/projects/PR-9001/anchor",
                                 json={"agent": AUTHOR, "github": ANCHOR})
        assert resp.status_code == 422, resp.get_json()
        assert "duplicate frontmatter key" in resp.get_json()["error"]
        assert disk(world, "PR-9001") == before

    @pytest.mark.parametrize("key", ["title", "turn_reason", "turn_by"])
    def test_unguarded_duplicate_keys_do_not_block_an_approved_advance(self, world, key):
        world.gh.reviews = [rv("R1", "example-gh-colleague", "APPROVED")]
        world.seed()
        with_dup(world, "PR-9001", key)
        assert world.advance().status_code == 201

    def test_a_clean_baton_still_advances(self, world):
        world.gh.reviews = [rv("R1", "example-gh-colleague", "APPROVED")]
        world.seed()
        assert world.advance().status_code == 201


# ---------------------------------------------------------------------------
# R5 -- non-string values: 422 naming route + field, never 500, never a write
# ---------------------------------------------------------------------------

NON_STRINGS = [["a"], {"k": "v"}, 7, 1.5, True, False, [], {}, 0]
BOOL_FIELDS = {"release": ["done"], "merge": ["forced"]}


def _nonstring_cells():
    out = []
    for route, kinds in FIELD_KINDS.items():
        for field in kinds:
            out.append((route, field))
        for field in BOOL_FIELDS.get(route, []):
            out.append((route, field))
    return out


@pytest.mark.parametrize("route,field", _nonstring_cells(),
                         ids=[f"{r}.{f}" for r, f in _nonstring_cells()])
def test_non_string_values_are_422_naming_route_and_field(world, route, field):
    suffix, base = BASES[route]
    bad_values = NON_STRINGS if field not in BOOL_FIELDS.get(route, []) else \
        ["yes", "false", 1, ["x"], {"a": 1}, "0"]
    failures = []
    for i, val in enumerate(bad_values):
        pid = SEEDS["claimed" if route in ("claim", "release") else "fairy"](world, i)
        body = dict(base)
        body[field] = val
        before, seq0 = disk(world, pid), seq_state(world)
        resp = post_safe(world, f"/api/projects/{pid}/{suffix}", body)
        tag = f"[{route}.{field}={val!r} -> {resp.status_code}]"
        err = (resp.get_json() or {}).get("error", "")
        if resp.status_code != 422:
            failures.append(f"{tag} expected 422: {err!r}")
        elif route_label(suffix) not in err or f"'{field}'" not in err:
            failures.append(f"{tag} 422 does not name route+field: {err!r}")
        if disk(world, pid) != before or seq_state(world) != seq0:
            failures.append(f"{tag} wrote / consumed a seq")
    assert not failures, "\n" + "\n".join(failures)


@pytest.mark.parametrize("val", NON_STRINGS, ids=[repr(v) for v in NON_STRINGS])
def test_init_non_string_fields_are_422(world, val):
    failures = []
    for field in ("agent", "project_id", "title", "status", "turn", "turn_reason",
                  "github", "colleague", "author", "phase", "transition_phase"):
        body = init_body("PR-9150")
        body[field] = val
        seq0 = seq_state(world)
        resp = post_safe(world, "/api/projects", body)
        err = (resp.get_json() or {}).get("error", "")
        if resp.status_code != 422 or "/api/projects" not in err or f"'{field}'" not in err:
            failures.append(f"[init.{field}={val!r} -> {resp.status_code}] {err!r}")
        if disk(world, "PR-9150") is not None or seq_state(world) != seq0:
            failures.append(f"[init.{field}={val!r}] wrote")
    assert not failures, "\n" + "\n".join(failures)


def test_init_participants_must_be_a_string_or_list_of_strings(world):
    failures = []
    for val in (7, {"a": 1}, [["x"]], [1], [None, {"a": 1}], True):
        body = init_body("PR-9151")
        body["participants"] = val
        resp = post_safe(world, "/api/projects", body)
        err = (resp.get_json() or {}).get("error", "")
        if resp.status_code != 422 or "'participants'" not in err:
            failures.append(f"[participants={val!r} -> {resp.status_code}] {err!r}")
    assert not failures, "\n" + "\n".join(failures)


@pytest.mark.parametrize("body", [["a"], "str", 5, 1.5, True], ids=repr)
@pytest.mark.parametrize("suffix", sorted({s for s, _ in BASES.values()} | {"init"}))
def test_non_object_json_body_is_400_not_500(world, suffix, body):
    url = "/api/projects" if suffix == "init" else "/api/projects/PR-9001/" + suffix
    world.seed()
    resp = post_safe(world, url, body)
    assert resp.status_code == 400, (suffix, body, resp.status_code, resp.get_json())


def test_null_is_absent_not_an_error(world):
    world.seed("PR-9001", phase="fairy", turn=AUTHOR)
    resp = world.client.post("/api/projects/PR-9001/flip", json={
        "agent": AUTHOR, "to_agent": COLLEAGUE, "reason": "please review",
        "phase": None, "transition_phase": None})
    assert resp.status_code == 201, resp.get_json()
    resp = world.client.post("/api/projects/PR-9001/flip", json={
        "agent": COLLEAGUE, "to_agent": AUTHOR, "reason": None})
    assert resp.status_code == 400  # required field absent, exactly as before


def test_empty_after_normalisation_is_a_400_required_field(world):
    world.seed("PR-9001", phase="fairy", turn=AUTHOR)
    for sep in SEPS.values():
        resp = world.client.post("/api/projects/PR-9001/flip", json={
            "agent": AUTHOR, "to_agent": COLLEAGUE, "reason": sep * 3})
        assert resp.status_code == 400, (repr(sep), resp.get_json())


def test_reason_normalisation_keeps_quotes_and_backslashes_exactly(world):
    world.seed("PR-9001", phase="fairy", turn=AUTHOR)
    resp = world.client.post("/api/projects/PR-9001/flip", json={
        "agent": AUTHOR, "to_agent": COLLEAGUE, "reason": 'say "hi" \\ and\ngo\t\x00ok'})
    assert resp.status_code == 201
    raw = universal(disk(world, "PR-9001"))
    assert 'turn_reason: "say "hi" \\ and go\t ok"' in raw
    assert not (UNSAFE_CHARS & set(raw))


def test_multi_line_reason_from_an_agent_is_kept_readable(world):
    world.seed("PR-9001", phase="fairy", turn=AUTHOR)
    resp = world.client.post("/api/projects/PR-9001/flip", json={
        "agent": AUTHOR, "to_agent": COLLEAGUE, "reason": "line one\nline two\r\n\r\nline three"})
    assert resp.status_code == 201
    raw = universal(disk(world, "PR-9001"))
    assert "line one line two line three" in raw


# ---------------------------------------------------------------------------
# Write-layer backstop (R1 last resort): a future writer cannot bypass the route
# ---------------------------------------------------------------------------

class TestWriteLayerBackstop:
    @pytest.mark.parametrize("sep", [v for k, v in SEPS.items()], ids=list(SEPS))
    def test_update_frontmatter_refuses_a_value_with_a_separator(self, sep):
        text = "---\nproject: PR-1\nphase: fairy\n---\nbody\n"
        with pytest.raises(ValueError):
            md.update_frontmatter(text, {"title": f"a{sep}phase: merge"})

    def test_update_frontmatter_refuses_a_key_with_a_separator(self):
        text = "---\nproject: PR-1\n---\nbody\n"
        with pytest.raises(ValueError):
            md.update_frontmatter(text, {"ti\ntle": "x"})

    def test_update_frontmatter_still_takes_clean_values_and_tabs(self):
        text = "---\nproject: PR-1\n---\nbody\n"
        assert "title: a\tb" in md.update_frontmatter(text, {"title": "a\tb"})

    @pytest.mark.parametrize("sep", [v for k, v in SEPS.items() if v != "\n"], ids=[k for k, v in SEPS.items() if v != "\n"])
    def test_append_turn_log_refuses_a_separator_in_an_entry(self, sep):
        with pytest.raises(ValueError):
            md.append_turn_log("# x\n", f"- 2026-10-01T00:00:00Z a → b: x{sep}phase: merge")

    def test_append_turn_log_refuses_a_non_log_continuation_line(self):
        with pytest.raises(ValueError):
            md.append_turn_log("# x\n", "- 2026-10-01T00:00:00Z a → b: x\nphase: merge")

    def test_append_turn_log_still_takes_the_legitimate_multi_line_entry(self):
        out = md.append_turn_log("# x\n", "- 2026-10-01T00:00:00Z a → b: x\n- 2026-10-01T00:00:00Z phase: 'peer' (transition)")
        assert out.count("\n- 2026") == 2

    @pytest.mark.parametrize("sep", ["\n", "\r", "\x0b", " "], ids=["LF", "CR", "VT", "LS"])
    def test_write_body_flip_refuses_before_any_seq_or_write(self, world, sep):
        world.seed("PR-9001", phase="fairy", turn=AUTHOR)
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        with pytest.raises(ValueError):
            _flip_wb(world.store, world.allocator, "PR-9001", to_agent=COLLEAGUE,
                     reason=f"x{sep}phase: merge", by_agent=AUTHOR)
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0

    def test_write_body_init_refuses_before_any_seq_or_write(self, world):
        seq0 = seq_state(world)
        with pytest.raises(ValueError):
            _init(world.store, world.allocator, "PR-9199", title="t\nphase: merge",
                  status="in-progress", turn=AUTHOR, participants=[AUTHOR],
                  turn_reason="start")
        assert disk(world, "PR-9199") is None and seq_state(world) == seq0

    def test_write_body_advance_refuses_without_writing(self, world):
        world.seed()
        before = disk(world, "PR-9001")
        with pytest.raises(ValueError):
            _advance_wb(world.store, world.allocator, "PR-9001", to_agent=SEAT,
                        reason="x\nphase: merge", by_agent=COLLEAGUE,
                        expected_anchor=ANCHOR, expected_colleague=COLLEAGUE,
                        resolve_phase=lambda f: "peer")
        assert disk(world, "PR-9001") == before
        # the lock was released: a normal write still works
        assert world.client.post("/api/projects/PR-9001/rename",
                                 json={"agent": AUTHOR, "new_title": "ok"}).status_code == 201


# ---------------------------------------------------------------------------
# Guard: a route that reads a NEW request field must join the matrix
# ---------------------------------------------------------------------------

def _route_blocks():
    src = inspect.getsource(server_mod.create_app)
    blocks = {}
    for m in re.finditer(
            r'@app\.route\("/api/projects(?:/<pid>/(\w+))?", methods=\["POST"\]\)(.*?)(?=\n    @app\.route|\n    return app)',
            src, re.S):
        blocks[m.group(1) or "init"] = m.group(2)
    return blocks


def _covered_fields():
    cov = {"init": set(INIT_KINDS)}
    for route, kinds in FIELD_KINDS.items():
        suffix = BASES[route][0]
        cov.setdefault(suffix, set()).update(kinds)
        cov[suffix].update(BOOL_FIELDS.get(route, []))
    return cov


def test_every_post_route_reads_its_fields_through_the_shared_helper_and_is_covered():
    blocks = _route_blocks()
    cov = _covered_fields()
    assert set(blocks) == set(cov), (set(blocks) ^ set(cov))
    for suffix, text in blocks.items():
        # no handler may read a client field around the shared sanitising helper
        raw_reads = re.findall(r'\bdata\.get\(\s*"(\w+)"|\bdata\[\s*"(\w+)"\s*\]', text)
        if suffix == "init":
            # EXACTLY one allowed raw read: init's ``data.get("project_id")`` (#3286).
            # The id becomes a file name, so the CLI's rule is applied (fullmatch) to
            # the UNSTRIPPED value as sent; _read_ident strips, so the check cannot run
            # on the helper's output. _read_fields runs first and already refuses
            # control chars and non-strings. The allow-list must not outlive the check:
            assert "_is_valid_project_id(raw_project_id)" in text, (
                "init's raw project_id read is allowed only alongside its validation call")
            assert raw_reads.count(("project_id", "")) == 1, raw_reads
            raw_reads = [r for r in raw_reads if r != ("project_id", "")]
        assert not raw_reads, f"{suffix} reads request fields outside the shared helper: {raw_reads}"
        spec_fields = set(re.findall(r'"(\w+)"\s*:\s*"(?:id|idl|text|bool|list)', text))
        assert spec_fields, f"{suffix}: no fields found via the shared helper"
        assert spec_fields <= cov[suffix], (suffix, spec_fields - cov[suffix])
        assert cov[suffix] <= spec_fields, (suffix, cov[suffix] - spec_fields)


@pytest.mark.parametrize("sep_quoted", ["%0A", "%0D", "%0B", "%00", "%7F", "%C2%85", "%E2%80%A8"])
@pytest.mark.parametrize("route", sorted(BASES))
def test_a_project_id_in_the_url_with_a_control_character_is_422(world, route, sep_quoted):
    """The project id is an identifier too: a URL-encoded line break in the path is
    refused (naming route and field) before the baton is even looked up."""
    suffix, base = BASES[route]
    world.seed("PR-9001", phase="fairy", turn=AUTHOR)
    before, seq0 = disk(world, "PR-9001"), seq_state(world)
    resp = post_safe(world, f"/api/projects/PR-9001{sep_quoted}phase:%20merge/{suffix}", dict(base))
    assert resp.status_code == 422, (route, sep_quoted, resp.status_code, resp.get_json())
    err = resp.get_json()["error"]
    assert route_label(suffix) in err and "'project_id'" in err
    assert disk(world, "PR-9001") == before and seq_state(world) == seq0
