"""Tests for the session-checkpoint fields + WS1 node-injection block
(#2026 WS1) in the SessionStart hook.

Two mechanisms under test, both living on engram-session-start-hook.py:

  - write_marker(): owns `session_started_at` (write-once) and
    `injected_through` (carried-forward, advanced only by main() after render).
  - session_checkpoint_block(engram_home, injected_through): pure-read render
    of "nodes created since injected_through", property-excluding blank
    renders (claim/recall_summary both empty) rather than type-excluding.

Mirrors the loading conventions of test_session_start_hook_block_order.py /
test_session_start_hook_focus_render.py (fresh module import per test via
importlib, in-memory sqlite fixture DB, main() driven via stdin+stdout
capture).
"""

import importlib.util
import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(engram_home: Path):
    """Load the session-start hook with ENGRAM_HOME pointing at engram_home."""
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ.pop("ENGRAM_RUNTIME_DIR", None)
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in ("engram-session-start-hook", "engram_session_start_hook"):
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # engram-alpha#127: pin detect_harness() to "claude-code" (measured
    # budget=9,500) -- this dev/source checkout has no baked platform.json
    # (see _harness.py's own docstring), so the hook's own detect_harness()
    # would otherwise return None and get_harness_budgets(None) would fall
    # through to UNMEASURED_HARNESS_FLOOR (4,000), which is smaller than
    # this file's fixtures were written against. Same discipline as
    # tests/test_lesson_tripwire_injection_bounds.py's own
    # _load_harness_budgets() docstring.
    module.detect_harness = lambda: "claude-code"
    # engram-alpha#127: stub out the tool-capability-index block so these
    # tests are not incidentally coupled to the REAL checked-in
    # tool_capability_index.txt artifact's size (measured 11,049 bytes on
    # this checkout -- alone bigger than the hook's own 9,500-char TOTAL
    # budget, which would starve every lower-priority block sorted after
    # it, including the calibration block this file's own ordering tests
    # check against).
    module.tool_capability_index_block = lambda: ""
    return module


def _make_nodes_db(engram_home: Path, rows: list[dict]) -> None:
    """Create knowledge.db with a nodes table carrying exactly the columns
    session_checkpoint_block() and focus_block() read. Each row dict may
    supply any subset of {id, type, claim, recall_summary, created_at,
    is_current, status}; missing keys default to NULL/None (is_current to 1,
    status to 'active') so callers can express "blank" rows tersely.

    `is_current` and `status` are BOTH required by the liveness tag, and the
    default pair is deliberate: on a real graph a SUPERSEDED node keeps
    status='active' while is_current drops to 0, so a fixture that defaulted
    status alone would make the superseded case untestable.
    """
    db_path = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT,
            claim TEXT,
            recall_summary TEXT,
            created_at TEXT,
            focused_at TEXT,
            focus_reason TEXT,
            is_current INTEGER,
            status TEXT
        )
        """
    )
    for row in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, recall_summary, created_at, "
            "is_current, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("id"),
                row.get("type"),
                row.get("claim"),
                row.get("recall_summary"),
                row.get("created_at"),
                row.get("is_current", 1),
                row.get("status", "active"),
            ),
        )
    conn.execute("PRAGMA user_version = 1")  # #3741: a real store carries a schema version; the flag-OFF calibration block now runs only on one
    conn.commit()
    conn.close()


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def _mock_no_engram_client():
    return patch.dict("sys.modules", {"engram_client": None})


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    return home


def _read_marker(engram_home: Path, session_id: str) -> dict:
    with open(engram_home / "sessions" / f"{session_id}.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. session_started_at survives a second SessionStart (not clobbered).
# ---------------------------------------------------------------------------

def test_session_started_at_survives_second_session_start(engram_home):
    mod = _load_hook_module(engram_home)
    hook_input = {
        "session_id": "sess-checkpoint-1",
        "transcript_path": "/tmp/t.jsonl",
        "source": "startup",
    }
    first = mod.write_marker(hook_input)
    assert first["session_started_at"] == first["started_at"]

    # Second SessionStart for the SAME session_id (e.g. a resume). started_at
    # advances (it's overwritten every call); session_started_at must not.
    second = mod.write_marker({**hook_input, "source": "resume"})
    assert second["session_started_at"] == first["session_started_at"], (
        "session_started_at must be carried forward verbatim, never clobbered "
        "by a later SessionStart for the same session_id"
    )


# ---------------------------------------------------------------------------
# 2. injected_through moves on source=compact, NOT on source=startup/resume.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["startup", "resume"])
def test_injected_through_does_not_advance_on_non_compact_source(engram_home, source):
    mod = _load_hook_module(engram_home)
    session_id = f"sess-no-advance-{source}"
    hook_input = {
        "session_id": session_id,
        "transcript_path": "/tmp/t.jsonl",
        "source": source,
    }
    # First call seeds injected_through (absent -> seed fires on ANY source).
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            _run_hook_main(mod, hook_input)
    seeded = _read_marker(engram_home, session_id)["injected_through"]
    assert seeded is not None

    # Second call, same non-compact source: injected_through must NOT move.
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            _run_hook_main(mod, hook_input)
    after_second = _read_marker(engram_home, session_id)["injected_through"]
    assert after_second == seeded, (
        f"injected_through moved on source={source!r} — it must move ONLY on "
        "source=='compact'."
    )


def test_injected_through_advances_on_compact_source(engram_home):
    mod = _load_hook_module(engram_home)
    session_id = "sess-advance-compact"
    startup_input = {
        "session_id": session_id,
        "transcript_path": "/tmp/t.jsonl",
        "source": "startup",
    }
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            _run_hook_main(mod, startup_input)
    seeded = _read_marker(engram_home, session_id)["injected_through"]
    assert seeded is not None

    compact_input = {**startup_input, "source": "compact"}
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            _run_hook_main(mod, compact_input)
    advanced = _read_marker(engram_home, session_id)["injected_through"]
    assert advanced != seeded, "injected_through must advance on source=='compact'"
    assert advanced > seeded


# ---------------------------------------------------------------------------
# 3. Legacy marker (neither field) upgrades without error, doesn't dump the
#    whole graph.
# ---------------------------------------------------------------------------

def test_legacy_marker_upgrades_without_error_and_no_full_dump(engram_home):
    session_id = "sess-legacy"
    # Pre-seed a legacy marker file with neither field, plus a very old
    # started_at, mimicking a pre-#2026 marker.
    legacy_marker = {
        "session_id": session_id,
        "transcript_path": "/tmp/t.jsonl",
        "source": "startup",
        "cwd": "/home/x",
        "started_at": "2020-01-01T00:00:00+00:00",
        "role": "interactive",
        "purpose": "Interactive session with user",
    }
    (engram_home / "sessions" / f"{session_id}.json").write_text(json.dumps(legacy_marker))

    # A node that predates the legacy started_at — if injected_through ever
    # fell back to "beginning of time" this would render.
    _make_nodes_db(engram_home, [
        {"id": "ob_0001", "type": "observation_factual", "claim": "Ancient node.",
         "created_at": "2019-01-01T00:00:00Z"},
    ])

    mod = _load_hook_module(engram_home)
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            result = _run_hook_main(mod, {
                "session_id": session_id,
                "transcript_path": "/tmp/t.jsonl",
                "source": "startup",
            })

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Ancient node" not in ctx, (
        "missing injected_through must render NOTHING this once, never fall back "
        "to 'beginning of time' and dump the whole graph"
    )

    upgraded = _read_marker(engram_home, session_id)
    assert upgraded["session_started_at"] is not None
    assert upgraded["injected_through"] is not None, "injected_through must be seeded after the empty render"


# ---------------------------------------------------------------------------
# 4. Window selection: nodes at/after injected_through in; before, out.
# ---------------------------------------------------------------------------

def test_window_selection_at_and_after_included_before_excluded(engram_home):
    _make_nodes_db(engram_home, [
        {"id": "ob_0001", "type": "observation_factual", "claim": "Before window.",
         "created_at": "2026-08-14T00:00:00Z"},
        {"id": "ob_0002", "type": "observation_factual", "claim": "At window boundary.",
         "created_at": "2026-08-15T00:00:00Z"},
        {"id": "ob_0003", "type": "observation_factual", "claim": "After window.",
         "created_at": "2026-08-15T12:00:00Z"},
    ])
    mod = _load_hook_module(engram_home)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert "ob_0001" not in text
    assert "ob_0002" in text
    assert "ob_0003" in text


# ---------------------------------------------------------------------------
# 5 & 6. Property-based exclusion, not type-based.
# ---------------------------------------------------------------------------

def test_blank_node_excluded_regardless_of_type(engram_home):
    _make_nodes_db(engram_home, [
        {"id": "ev_0001", "type": "evidence", "claim": None, "recall_summary": None,
         "created_at": "2026-08-15T01:00:00Z"},
    ])
    mod = _load_hook_module(engram_home)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert "ev_0001" not in text
    assert text == ""


def test_evidence_type_with_claim_is_included():
    """The test that fails under a type check and passes under a property
    check — the point of the rule (Borges, #5504)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        engram_home = Path(td)
        (engram_home / "sessions").mkdir()
        _make_nodes_db(engram_home, [
            {"id": "ev_0002", "type": "evidence", "claim": "Evidence that carries its own claim.",
             "created_at": "2026-08-15T01:00:00Z"},
        ])
        mod = _load_hook_module(engram_home)
        text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
        assert "ev_0002" in text
        assert "Evidence that carries its own claim." in text


# ---------------------------------------------------------------------------
# 7. recall_summary preferred over claim; claim[:100] used when absent.
# ---------------------------------------------------------------------------

def test_recall_summary_preferred_over_claim(engram_home):
    _make_nodes_db(engram_home, [
        {"id": "ob_0010", "type": "observation_factual", "claim": "The full claim text, long form.",
         "recall_summary": "Short curated summary.", "created_at": "2026-08-15T01:00:00Z"},
    ])
    mod = _load_hook_module(engram_home)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert "Short curated summary." in text
    assert "The full claim text, long form." not in text


def test_liveness_tag_superseded_keys_on_is_current_not_status(engram_home):
    """A superseded node is tagged, and the tag cannot be derived from status.

    This is the case that discriminates a correct implementation from a
    plausible one. On a real graph a superseded node keeps `status='active'`
    while `is_current` drops to 0 — measured, 175 nodes in that state — so an
    implementation keying on `status` would render it as an ordinary live
    entry. The fixture reproduces that exact pairing on purpose: an
    is_current=0 / status='active' row MUST come back tagged.
    """
    _make_nodes_db(engram_home, [
        {"id": "ob_0020", "type": "observation_factual", "claim": "Replaced claim.",
         "created_at": "2026-08-15T01:00:00Z", "is_current": 0, "status": "active"},
        {"id": "ob_0021", "type": "observation_factual", "claim": "Live claim.",
         "created_at": "2026-08-15T01:01:00Z", "is_current": 1, "status": "active"},
    ])
    mod = _load_hook_module(engram_home)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert "[ob_0020] (observation_factual) [superseded] Replaced claim." in text
    # The live node carries no tag at all — the tag must not fire on status.
    assert "[ob_0021] (observation_factual) Live claim." in text
    assert "[ob_0021] (observation_factual) [superseded]" not in text


def test_liveness_tag_retracted_is_distinguished_from_superseded(engram_home):
    """Retraction is the one case that moves `status`, so it gets its own tag.

    Conflating the two would be a real loss: a retracted claim was never
    valid, a superseded one was valid when written. The post-compaction
    reader needs to tell those apart before acting on either.
    """
    _make_nodes_db(engram_home, [
        {"id": "ob_0030", "type": "observation_factual", "claim": "Never was true.",
         "created_at": "2026-08-15T01:00:00Z", "is_current": 0, "status": "retracted"},
    ])
    mod = _load_hook_module(engram_home)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert "[retracted]" in text
    assert "[superseded]" not in text


def test_non_current_nodes_are_included_not_filtered(engram_home):
    """Superseded/retracted nodes stay IN the window, deliberately.

    A claim filed and then replaced inside the same burst is part of what
    happened; filtering it would erase exactly the self-correction the
    post-compaction self most needs to see. The tag is what keeps that
    inclusion honest — the alternative (drop them) loses information, and
    the naive alternative (include untagged) asserts a dead claim is live.
    """
    _make_nodes_db(engram_home, [
        {"id": "ob_0040", "type": "observation_factual", "claim": "First take.",
         "created_at": "2026-08-15T01:00:00Z", "is_current": 0, "status": "active"},
        {"id": "ob_0041", "type": "observation_factual", "claim": "Corrected take.",
         "created_at": "2026-08-15T01:05:00Z", "is_current": 1, "status": "active"},
    ])
    mod = _load_hook_module(engram_home)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert "ob_0040" in text and "ob_0041" in text
    assert "2 node(s)" in text


def test_claim_truncated_to_100_when_recall_summary_absent(engram_home):
    long_claim = "X" * 150
    _make_nodes_db(engram_home, [
        {"id": "ob_0011", "type": "observation_factual", "claim": long_claim,
         "recall_summary": None, "created_at": "2026-08-15T01:00:00Z"},
    ])
    mod = _load_hook_module(engram_home)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert ("X" * 100) in text
    assert ("X" * 101) not in text


# ---------------------------------------------------------------------------
# 8. Cap renders "... +N more" rather than silently dropping.
# ---------------------------------------------------------------------------

def test_cap_renders_explicit_more_line(engram_home):
    """Elision is explicit, and the assertion is written against the constant.

    Deliberately derives the fixture size and the expected overflow from
    CHECKPOINT_LIST_CAP rather than hard-coding either. An earlier version
    pinned the literal 100 in an assertion, which meant a considered change
    to the cap failed this test for the wrong reason — the test would have
    been reporting "the cap changed", not "elision broke".
    """
    mod = _load_hook_module(engram_home)
    cap = mod.CHECKPOINT_LIST_CAP
    overflow = 4
    rows = [
        {"id": f"ob_{i:04d}", "type": "observation_factual", "claim": f"Node {i}.",
         "created_at": f"2026-08-15T01:{i % 60:02d}:{i % 60:02d}Z"}
        for i in range(1, cap + overflow + 1)
    ]
    _make_nodes_db(engram_home, rows)
    text = mod.session_checkpoint_block(str(engram_home), "2026-08-15T00:00:00Z")
    assert f"... +{overflow} more" in text, text[-200:]
    # The header still reports the TRUE total, not the displayed count —
    # a cap that also shrank the count would hide the elision it announces.
    assert f"{cap + overflow} node(s)" in text


def test_corrupt_marker_reseeds_rather_than_crashing(engram_home):
    """A torn/corrupt marker must degrade to a reseed, never to an exception.

    This is the failure the atomic write in _persist_injected_through exists to
    prevent, asserted from the other side: if a marker ever DOES arrive
    corrupt, write_marker() must treat it as absent and carry on rather than
    raising into SessionStart. Note the cost being accepted here — a reseed
    silently drops the window, which is a GAP, the one direction this
    workstream rules out. That is precisely why the write is atomic and why
    this test asserts only the degradation path, not that the path is benign.
    """
    sessions_dir = engram_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sess-corrupt.json").write_text('{"session_id": "sess-corr')  # truncated
    mod = _load_hook_module(engram_home)
    marker = mod.write_marker({
        "session_id": "sess-corrupt", "transcript_path": "/tmp/t.jsonl", "source": "compact",
    })
    assert marker is not None, "a corrupt prior marker must not fail the write"
    assert marker["injected_through"] is None, "corrupt prior state is treated as absent"
    assert marker["session_started_at"] == marker["started_at"], (
        "with nothing readable to carry forward, session_started_at falls back to now()"
    )


def test_persist_injected_through_mid_write_failure_leaves_original_intact(engram_home):
    """The property atomicity actually buys: if the write is interrupted,
    the marker on disk is still the OLD one, not a torn/partial one.

    Simulates interruption by making json.dumps raise through
    _persist_injected_through's call into atomic_write_json. Asserts:
      1. _persist_injected_through itself does not raise (fail-soft, per its
         own docstring -- the outer except Exception: pass must still hold).
      2. The marker file on disk afterward is BYTE-IDENTICAL to before the
         call -- still valid JSON, still carrying the OLD injected_through.
      3. No leftover *.tmp file in the sessions directory.
    """
    session_id = "sess-mid-write-kill"
    marker_path = engram_home / "sessions" / f"{session_id}.json"
    original_marker = {
        "session_id": session_id,
        "transcript_path": "/tmp/t.jsonl",
        "source": "startup",
        "cwd": "/home/x",
        "started_at": "2026-08-15T00:00:00+00:00",
        "role": "interactive",
        "purpose": "Interactive session with user",
        "session_started_at": "2026-08-15T00:00:00+00:00",
        "injected_through": "2026-08-15T00:00:00Z",
    }
    marker_path.write_text(json.dumps(original_marker))
    original_bytes = marker_path.read_bytes()

    mod = _load_hook_module(engram_home)
    with patch("_hooklib.json.dumps", side_effect=RuntimeError("simulated mid-write kill")):
        mod._persist_injected_through(str(engram_home), session_id, "2026-08-15T99:99:99Z")
        # must not raise -- if it does, the fail-soft contract is broken

    assert marker_path.read_bytes() == original_bytes, (
        "a failed write must leave the original marker completely untouched"
    )
    assert not list((engram_home / "sessions").glob("*.tmp")), (
        "a failed write must not leave a temp file behind"
    )


# ---------------------------------------------------------------------------
# 9. Block ordering: new block appears before the calibration block.
# ---------------------------------------------------------------------------

_CANNED_CONF_ALL = {
    "by_type": {
        "observation_factual": {"n": 5, "mean": 0.72, "p25": 0.65, "p50": 0.72, "p75": 0.80},
    },
}
_CANNED_CONF_7D = {"by_type": {}}


def _make_mock_engram_client():
    mock_client = MagicMock()

    def _call_side_effect(tool_name, arguments=None):
        if tool_name != "engram_stats":
            return {}
        try:
            params = json.loads((arguments or {}).get("payload_json", "{}"))
        except (json.JSONDecodeError, AttributeError):
            params = {}
        if params.get("mode") == "7-turn":
            return {"confidence": _CANNED_CONF_7D}
        return {"confidence": _CANNED_CONF_ALL, "memory": {"current_turn": 3, "tier_sizes": {}}}

    mock_client.call.side_effect = _call_side_effect
    return mock_client


def test_checkpoint_block_precedes_calibration_block(engram_home):
    session_id = "sess-order"
    # Seed injected_through via a first SessionStart, then create a node and
    # run a second SessionStart so the injection block actually renders.
    mod = _load_hook_module(engram_home)
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            _run_hook_main(mod, {
                "session_id": session_id, "transcript_path": "/tmp/t.jsonl", "source": "startup",
            })
    seeded = _read_marker(engram_home, session_id)["injected_through"]

    _make_nodes_db(engram_home, [
        {"id": "ob_0099", "type": "observation_factual", "claim": "Ordering-test node.",
         "created_at": seeded},
    ])

    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            result = _run_hook_main(mod, {
                "session_id": session_id, "transcript_path": "/tmp/t.jsonl", "source": "startup",
            })

    ctx = result["hookSpecificOutput"]["additionalContext"]
    injected_through_idx = ctx.find("Nodes created since checkpoint")
    calib_at = ctx.find("ENGRAM Calibration")
    assert injected_through_idx != -1, f"checkpoint block missing entirely: {ctx!r}"
    assert calib_at != -1, f"calibration block missing entirely: {ctx!r}"
    assert injected_through_idx < calib_at
