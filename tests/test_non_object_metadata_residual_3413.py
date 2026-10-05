"""Residual non-object `nodes.metadata` sites found in reviewer round 1 of
#3423 (#3413): the `engram-pkg` script, the user-identity hook, the viz
calibration layer and the similarity advisory.

Contract (written before the routing):
  R1 `engram-pkg`'s `_render_origin(node_row)` never raises (its docstring's
     promise): for a metadata blob of `[1,2]`, `"str"`, `5`, `null` it returns
     `{}`; for a JSON-object blob it returns the object's `origin` value
     exactly as before.
"""

import importlib.machinery
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

NON_OBJECT_BLOBS = ["[1,2]", '"str"', "5", "null"]


def _load_pkg_cli():
    cli_path = os.path.join(ROOT, "src", "engram", "tools", "engram-pkg", "engram-pkg")
    spec = importlib.util.spec_from_loader(
        "engram_pkg_cli_residual_3413",
        importlib.machinery.SourceFileLoader("engram_pkg_cli_residual_3413", cli_path),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- item 1

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_render_origin_non_object_blob_returns_empty(blob):
    cli = _load_pkg_cli()
    assert cli._render_origin({"metadata": blob}) == {}


DICT_BATTERY = [
    ({"metadata": None}, {}),
    ({"metadata": ""}, {}),
    ({"metadata": "{}"}, {}),
    ({"metadata": "not json"}, {}),
    ({"metadata": json.dumps({"origin": {"src": "x", "n": 1}})}, {"src": "x", "n": 1}),
    ({"metadata": json.dumps({"origin": None})}, {}),
    ({"metadata": json.dumps({"other": 1})}, {}),
    ({}, {}),
]


@pytest.mark.parametrize("row,expected", DICT_BATTERY)
def test_render_origin_dict_blobs_unchanged(row, expected):
    cli = _load_pkg_cli()
    assert cli._render_origin(row) == expected


# ---------------------------------------------------------------- item 2
# R2 `detect_speaker(prompt, people)` (user-identity hook): a person whose
#    metadata blob is `[1,2]`, `5`, `null` or `"str"` is treated as `{}` (no
#    name, no aliases: it can never match a prefix); it must not raise, and a
#    later person with a dict blob must still be matched. Dict blobs behave
#    as before.
# R3 viz `get_calibration_data(db_path)`: a derivation / observation with a
#    non-object blob lands in the same bucket an INVALID-JSON blob already
#    lands in (`legacy_untyped` / `external`) and does not raise; dict blobs
#    bucket exactly as before.

import sqlite3
import importlib.util as _ilu

_HOOK_PATH = os.path.join(ROOT, "src", "engram", "hooks", "claude",
                          "engram-user-identity-hook.py")


def _load_identity_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    sys.modules.pop("engram_user_identity_hook_residual_3413", None)
    spec = _ilu.spec_from_file_location("engram_user_identity_hook_residual_3413", _HOOK_PATH)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _person(pid, blob):
    return {"id": pid, "metadata": blob, "logical_chain": "desc"}


@pytest.mark.parametrize("blob", ["[1,2]", "5", "null", '"str"'])
def test_detect_speaker_skips_a_non_object_blob_and_still_matches_later(blob, tmp_path, monkeypatch):
    hook = _load_identity_hook(tmp_path, monkeypatch)
    good = _person("pn_2", json.dumps({"name": "Lei", "aliases": ["L"], "role": "owner"}))
    got = hook.detect_speaker("Lei: hello", [_person("pn_1", blob), good])
    assert got is not None and got["person_id"] == "pn_2" and got["role"] == "owner"
    assert hook.detect_speaker("Nobody: hi", [_person("pn_1", blob)]) is None


def test_detect_speaker_dict_blobs_unchanged(tmp_path, monkeypatch):
    hook = _load_identity_hook(tmp_path, monkeypatch)
    people = [
        _person("pn_1", json.dumps({"name": "Ann", "aliases": ["A"]})),
        _person("pn_2", json.dumps({"name": "Lei", "aliases": ["L"], "role": "owner"})),
        _person("pn_3", "{}"),
    ]
    assert hook.detect_speaker("a: x", people) == {
        "person_id": "pn_1", "name": "Ann", "role": "", "description": "desc",
        "matched_prefix": "a"}
    assert hook.detect_speaker("L : x", people)["person_id"] == "pn_2"
    assert hook.detect_speaker("Zed: x", people) is None
    assert hook.detect_speaker("no prefix", people) is None


def _calib(tmp_path, blobs):
    from viz.data import calibration
    db = str(tmp_path / "k.db")
    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, created_at TEXT NOT NULL,"
        " confidence REAL, is_current INTEGER DEFAULT 1, status TEXT, quote_type TEXT, metadata TEXT)")
    for i, (typ, conf, blob) in enumerate(blobs):
        conn.execute("INSERT INTO nodes VALUES (?,?,?,?,1,NULL,NULL,?)",
                     (f"n_{i}", typ, "2020-01-01T00:00:00Z", conf, blob))
    conn.commit()
    conn.close()
    return calibration.get_calibration_data(db)


@pytest.mark.parametrize("blob", ["[1,2]", "5", "null", '"str"'])
def test_calibration_non_object_blob_buckets_like_invalid_json(blob, tmp_path):
    bad = _calib(tmp_path, [("derivation", 0.5, blob), ("observation_factual", 0.9, blob)])
    assert "error" not in bad, bad
    inv = _calib(tmp_path, [("derivation", 0.5, "not json"), ("observation_factual", 0.9, "not json")])
    assert bad["corpus"]["by_reasoning_type"] == inv["corpus"]["by_reasoning_type"] == \
        {"legacy_untyped": {"n": 1, "mean": 0.5}}
    assert bad["corpus"]["by_source_class"] == inv["corpus"]["by_source_class"] == \
        {"external": {"n": 1, "mean": 0.9}}


def test_calibration_dict_blobs_unchanged(tmp_path):
    out = _calib(tmp_path, [
        ("derivation", 0.5, json.dumps({"reasoning_type": "deductive_modus_ponens"})),
        ("derivation", 0.7, json.dumps({})),
        ("observation_factual", 0.9, json.dumps({"source_class": "user_stated"})),
        ("observation_factual", 0.8, json.dumps({})),
    ])
    assert out["corpus"]["by_reasoning_type"] == {
        "deductive_modus_ponens": {"n": 1, "mean": 0.5}, "legacy_untyped": {"n": 1, "mean": 0.7}}
    assert out["corpus"]["by_source_class"] == {
        "user_stated": {"n": 1, "mean": 0.9}, "external": {"n": 1, "mean": 0.8}}


# ---------------------------------------------------------------- item 3
# R4 `_similar_existing_matches` (advisory similarity lookup behind
#    `engram_add_lesson` / `engram_add_observation`): ONE hit whose metadata
#    blob is a non-object must not empty the whole lookup. With
#    `extra_metadata_keys` the bad-blob hit is kept with `""` for each key (the
#    value a dict blob lacking the key gets) and the OTHER hits are returned
#    with their real values. Dict blobs: unchanged. (Only the lesson caller
#    reads metadata keys; the observation caller reads columns only, so its
#    cell is a no-regression control that is green before and after.)

import shutil
import tempfile


@pytest.fixture
def sim_env():
    d = tempfile.mkdtemp(prefix="engram_test_nonobj_sim_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    from engram_core import _get_db
    conn = _get_db()

    def setmeta(nid, text):
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (text, nid))
        conn.commit()

    yield server, conn, setmeta
    shutil.rmtree(d, ignore_errors=True)


def _call(fn, **p):
    return json.loads(fn(payload_json=json.dumps(p)))


def _mint_obs(server, tag, claim):
    r = _call(
        server.engram_add_observation,
        url=f"https://example.com/{tag}", title=f"Src {tag}",
        quoted_text=f"{tag} verbatim quote text.",
        interpretation=f"{tag} interpretation text here.",
        claim=claim, quote_type="hard_data",
    )
    assert "observation_id" in r, r
    return r["observation_id"]


def _mint_lesson(server, tag, claim, nudge):
    oid = _mint_obs(server, "l" + tag, f"{tag} granite quarry output rose in spring.")
    r = _call(
        server.engram_add_lesson,
        claim=claim, incident_ids=oid, scaffolding_nudge=nudge,
        logical_chain="The single quarry incident shows seasonality confounds output.",
        situation_pattern="quarry output report received",
    )
    assert "lesson_id" in r, r
    return r, r["lesson_id"]


_LESSON_CLAIMS = [
    "When a quarry report arrives, check the season first.",
    "When a quarry report arrives, check the season twice.",
]
# the new claim's tokens are a subset of both, so the (legacy AND) FTS query hits both
_NEW_LESSON = "When a quarry report arrives, check the season."


def _ids(r):
    return {e["id"] for e in r.get("similar_existing_lessons", [])}


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_similar_lessons_survive_one_non_object_blob(sim_env, blob):
    server, conn, setmeta = sim_env
    _, a = _mint_lesson(server, "a", _LESSON_CLAIMS[0], "Nudge A.")
    _, b = _mint_lesson(server, "b", _LESSON_CLAIMS[1], "Nudge B.")
    setmeta(a, blob)
    r, _ = _mint_lesson(server, "c", _NEW_LESSON, "Nudge C.")
    assert _ids(r) == {a, b}, r.get("similar_existing_lessons")
    by_id = {e["id"]: e for e in r["similar_existing_lessons"]}
    assert by_id[b]["scaffolding_nudge"] == "Nudge B."
    assert by_id[a]["scaffolding_nudge"] == ""


def test_similar_lessons_dict_blobs_unchanged(sim_env):
    server, conn, setmeta = sim_env
    _, a = _mint_lesson(server, "a", _LESSON_CLAIMS[0], "Nudge A.")
    _, b = _mint_lesson(server, "b", _LESSON_CLAIMS[1], "Nudge B.")
    r, _ = _mint_lesson(server, "c", _NEW_LESSON, "Nudge C.")
    by_id = {e["id"]: e for e in r["similar_existing_lessons"]}
    assert set(by_id) == {a, b}
    assert by_id[a]["scaffolding_nudge"] == "Nudge A."
    assert by_id[b]["scaffolding_nudge"] == "Nudge B."


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_similar_observations_unaffected_by_a_non_object_blob(sim_env, blob):
    """No-regression control: the observation caller reads no metadata key."""
    server, conn, setmeta = sim_env
    a = _mint_obs(server, "a", "Granite quarry output rose sharply in the spring season.")
    b = _mint_obs(server, "b", "Granite quarry output rose steadily in the spring season.")
    setmeta(a, blob)
    r = _call(
        server.engram_add_observation,
        url="https://example.com/c", title="Src c",
        quoted_text="c verbatim quote text.", interpretation="c interpretation text here.",
        claim="Granite quarry output rose in the spring season.", quote_type="hard_data",
    )
    ids = {e["id"] for e in r.get("similar_existing", [])}
    assert {a, b} <= ids, r.get("similar_existing")
