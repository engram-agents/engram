"""#3432: ``baton init`` refuses a CASE VARIANT of an existing project id.

Contract (stated by the issue, asserted here before the implementation):
  * with ``umwelt-PR-954`` existing, init of ``UMWELT-PR-954`` -> 409, the
    message names the EXISTING spelling, and nothing is created;
  * an exact duplicate -> 409 with the unchanged ``project already exists: <id>``;
  * a genuinely new id -> 201 (control);
  * a closed/merged project still blocks a case variant (as it already blocks an
    exact duplicate);
  * no migration: a case-variant pair already in the store is left untouched.

The macOS default filesystem is case-INSENSITIVE, so a FileStore-backed test can
pass for the wrong reason (a path collision). The ``*_case_sensitive_store``
tests use a dict-backed store (case-sensitive everywhere) so the CODE path is
what is proved, and every message assertion checks the existing id's exact
spelling, which a filesystem collision would not produce.

Identities are SYNTHETIC.
"""

import sys

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import World, SENTINEL, AUTHOR, COLLEAGUE  # noqa: E402

from forum.coordination import init as _init, ProjectAlreadyExists  # noqa: E402
from forum.coordination.seq import SeqAllocator  # noqa: E402
from forum.coordination.store import CoordinationStore, ProjectRecord  # noqa: E402

EXISTING = "umwelt-PR-954"
VARIANT = "UMWELT-PR-954"
GH = "pr/example-org/example-repo/954"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _body(pid, github=GH):
    return {
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "r",
        "participants": [AUTHOR, COLLEAGUE, SENTINEL],
        "colleague": COLLEAGUE, "author": AUTHOR, "github": github,
    }


def _ids(world):
    return sorted(r.project_id for r in world.store.read_projects(active_only=False))


def test_case_variant_of_active_project_is_409_naming_existing(world):
    world.seed(EXISTING, github=GH)
    before = _ids(world)
    r = world.client.post("/api/projects", json=_body(VARIANT))
    assert r.status_code == 409, r.get_json()
    err = r.get_json()["error"]
    assert EXISTING in err  # the existing id's exact spelling
    assert VARIANT in err  # and the requested spelling
    assert "case-insensitive" in err
    assert _ids(world) == before  # nothing created


def test_case_variant_of_closed_project_is_409(world):
    world.seed(EXISTING, github=GH, status="merged")
    before = _ids(world)
    r = world.client.post("/api/projects", json=_body(VARIANT))
    assert r.status_code == 409, r.get_json()
    err = r.get_json()["error"]
    assert EXISTING in err and VARIANT in err
    assert _ids(world) == before


@pytest.mark.parametrize("existing,requested", [
    ("UMWELT-PR-954", "umwelt-PR-954"),  # existing upper, lower requested
    ("umwelt-PR-954", "UMWELT-PR-954"),  # existing lower, upper requested
])
def test_both_real_spellings_both_directions_name_both(world, existing, requested):
    world.seed(existing, github=GH)
    before = _ids(world)
    r = world.client.post("/api/projects", json=_body(requested))
    assert r.status_code == 409, r.get_json()
    err = r.get_json()["error"]
    assert f"requested '{requested}'" in err
    assert f"existing '{existing}'" in err
    assert "\n" not in err
    assert _ids(world) == before


def test_exact_duplicate_message_unchanged(world):
    world.seed(EXISTING, github=GH)
    r = world.client.post("/api/projects", json=_body(EXISTING))
    assert r.status_code == 409
    assert r.get_json()["error"] == f"project already exists: {EXISTING}"


def test_genuinely_new_id_is_created(world):
    world.seed(EXISTING, github=GH)
    r = world.client.post(
        "/api/projects", json=_body("umwelt-PR-955", github="pr/example-org/example-repo/955"))
    assert r.status_code == 201, r.get_json()


class _DictStore(CoordinationStore):
    """Case-sensitive in-memory store (independent of the host filesystem)."""

    def __init__(self):
        self.files = {}

    def read_projects(self, *, active_only=True):
        return [
            ProjectRecord(
                project_id=pid, title="", status="in-progress", turn="", turn_since="",
                turn_reason="", participants=[], seq=0, raw=raw)
            for pid, raw in self.files.items()
        ]

    def read_project(self, project_id):
        return self.files.get(project_id)

    def write_project(self, project_id, content, *, seq):
        self.files[project_id] = content

    def archive_project(self, project_id):
        raise NotImplementedError

    def read_dm_thread(self, *a, **k):
        raise NotImplementedError

    def append_dm(self, *a, **k):
        raise NotImplementedError

    def list_dm_threads(self, *a, **k):
        raise NotImplementedError

    def list_all_dm_threads(self, *a, **k):
        raise NotImplementedError

    def recover_max_seq(self):
        return 0


def _kw():
    return dict(title="t", status="in-progress", turn="a", participants=["a"], turn_reason="r")


def test_case_variant_refused_on_case_sensitive_store():
    s = _DictStore()
    alloc = SeqAllocator(recover=s.recover_max_seq)
    _init(s, alloc, EXISTING, **_kw())
    with pytest.raises(ProjectAlreadyExists) as exc:
        _init(s, alloc, VARIANT, **_kw())
    assert exc.value.existing_ids == [EXISTING]
    assert list(s.files) == [EXISTING]  # nothing created


def test_several_existing_variants_are_all_named_on_case_sensitive_store():
    s = _DictStore()
    s.files[EXISTING] = "a"
    s.files[VARIANT] = "b"
    alloc = SeqAllocator(recover=s.recover_max_seq)
    with pytest.raises(ProjectAlreadyExists) as exc:
        _init(s, alloc, "Umwelt-Pr-954", **_kw())
    assert sorted(exc.value.existing_ids) == sorted([EXISTING, VARIANT])


def test_exact_duplicate_on_case_sensitive_store_names_same_id():
    s = _DictStore()
    alloc = SeqAllocator(recover=s.recover_max_seq)
    _init(s, alloc, EXISTING, **_kw())
    with pytest.raises(ProjectAlreadyExists) as exc:
        _init(s, alloc, EXISTING, **_kw())
    assert exc.value.existing_ids == [EXISTING]


def test_existing_case_variant_pair_is_untouched_no_migration():
    """A pair already in the store stays as-is; exact-id lookup still works for both."""
    s = _DictStore()
    s.files[EXISTING] = "a"
    s.files[VARIANT] = "b"
    alloc = SeqAllocator(recover=s.recover_max_seq)
    with pytest.raises(ProjectAlreadyExists):
        _init(s, alloc, "Umwelt-Pr-954", **_kw())
    assert s.files == {EXISTING: "a", VARIANT: "b"}
    assert s.read_project(EXISTING) == "a" and s.read_project(VARIANT) == "b"


class _RacyAllocator(SeqAllocator):
    """Runs ``before_lock`` AFTER init's unlocked fast-path check, BEFORE the lock.

    ``init`` calls ``_refuse_existing_id`` (unlocked), builds the content, then
    enters ``allocator.locked()``. Hooking ``locked()`` therefore simulates a
    competing init landing in exactly that window, deterministically.
    """

    def __init__(self, recover, before_lock):
        super().__init__(recover=recover)
        self._before_lock = before_lock

    def locked(self):
        self._before_lock()
        return super().locked()


@pytest.mark.parametrize("landed", [VARIANT, EXISTING], ids=["case_variant", "exact_duplicate"])
def test_in_lock_recheck_catches_a_race_between_fast_path_and_lock(landed):
    """Pins the in-lock re-check: delete it and this goes red (the fast path
    saw an empty store, so only the re-check under the lock can refuse)."""
    s = _DictStore()
    alloc = _RacyAllocator(
        s.recover_max_seq, lambda: s.files.__setitem__(landed, "winner"))
    seq_before = alloc._seq
    request = "Umwelt-Pr-954" if landed == VARIANT else EXISTING
    with pytest.raises(ProjectAlreadyExists) as exc:
        _init(s, alloc, request, **_kw())
    assert exc.value.existing_ids == [landed]
    assert s.files == {landed: "winner"}  # the first baton is not overwritten
    assert alloc._seq == seq_before  # the refusal burned no seq
    # Public observable: the next genuine init gets the very next seq (+2 if burned).
    assert _init(s, alloc, "fresh-id-1", **_kw()) == seq_before + 1


def test_refusal_burns_no_seq_on_the_fast_path_too():
    s = _DictStore()
    alloc = SeqAllocator(recover=s.recover_max_seq)
    _init(s, alloc, EXISTING, **_kw())
    seq_after_create = alloc._seq
    with pytest.raises(ProjectAlreadyExists):
        _init(s, alloc, VARIANT, **_kw())
    assert alloc._seq == seq_after_create
    # Public observable: the next genuine init gets the very next seq (+2 if burned).
    assert _init(s, alloc, "fresh-id-1", **_kw()) == seq_after_create + 1
