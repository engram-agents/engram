"""Contract tests for tools/compact-request.py (part of #3640).

Contract-first (CLAUDE.md "Contract-first tests"): every assertion below was
written BEFORE the tool existed and was run red at base.

The tool is half A of agent-decided compaction at the nap boundary. After a nap
it assembles the post-compaction context deterministically and leaves two files
under ``$ENGRAM_HOME/compact/``:

    context.md       the text
    context.sha256   its digest
    request.json     the trigger, written LAST

The mod half (tools/mods/engram-compaction) consumes request.json; these cells
cover only the Python half, so CI needs no claude CLI.

Contract pinned (one cell or more each):
  P1  happy path: context holds the focus list (byte-equal to the SessionStart
      hook's own builder), the history arc, the warm-briefing arc, the loop
      marker's fields; request.json is written LAST with a matching sha256
  P2  rider: --backup-ok false, and each failed warm-briefing check (anchor
      count, empty arc, older than the checkpoint, checkpoint unknown) -> exit 3,
      NOTHING written, ONE stderr line naming the check
  P2b the checkpoint time comes from the last "## Nap" header of session_log.md;
      --checkpoint-at overrides it
  P3  absent pieces are NAMED inside the context, never skipped; still exit 0
  P4  a failure between context.md and request.json leaves NO request.json
  P5  an older request.json is replaced, never left beside the new one
  P6  loading the hook builders prints nothing and never writes the DB
  P7  size cap: context at the cap is written, one byte over is refused (exit 4)
  P8  no ``loop-wake`` text (any case, also HTML-escaped) in context.md; the
      loop marker's loop_prompt is excluded
  P9  the hook file is found in both the deployed (hooks/) and the source
      (hooks/claude/) layouts
  P10 tools/compact-request.py is a working entry point (symlink, CLI exit codes)
  P11 --clear: removes every pending file, exit 0; exit 4 when a removal fails, and only a failed
      removal of request.json leaves the request live; --session is ignored, and every run removes
      any older request whatever its session
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO / "src" / "engram" / "tools" / "compact-request.py"
LINK_PATH = REPO / "tools" / "compact-request.py"

SESSION = "sess-aaaa"
CHECKPOINT = "2020-01-01T00:00:00"
FOCUS_ID = "ob_" + "9001"  # built, never spelled: the no-shipped-node-ids gate is for tools/, kept here by habit
FOCUS_CLAIM = "Focus-render claim for the compaction cell."
FOCUS_REASON = "compaction thread"
ARC = "Today we built the request half.\nSecond paragraph of the arc."
HISTORY = "# 2026-10-11\n- shipped the thing\n"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_tool():
    spec = importlib.util.spec_from_file_location("compact_request_under_test", TOOL_PATH)
    assert spec and spec.loader, f"cannot load {TOOL_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool():
    return _load_tool()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_home(
    tmp_path: Path,
    *,
    focus: bool = True,
    arc: str | None = ARC,
    history: bool = True,
    loop: dict | None = None,
    checkpoint: str | None = CHECKPOINT,
    anchors: int = 1,
) -> Path:
    home = tmp_path / "engram-home"
    home.mkdir()
    if focus:
        conn = sqlite3.connect(str(home / "knowledge.db"))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, focused_at TEXT, "
            "focus_reason TEXT, is_current INTEGER)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, 1)",
            (FOCUS_ID, FOCUS_CLAIM, _now_iso(), FOCUS_REASON),
        )
        conn.commit()
        conn.close()
    body = "# Letter\nthe person's words, untouched\n\n"
    for _ in range(anchors):
        body += "## From this session\n"
        if arc is not None:
            body += arc + "\n"
        body += "\n" if anchors > 1 else ""
    if anchors == 0:
        body += "no anchor here\n"
    (home / "warm-briefing.md").write_text(body, encoding="utf-8")
    if history:
        (home / "history").mkdir()
        (home / "history" / "2026-10-11.md").write_text(HISTORY, encoding="utf-8")
    if loop is not None:
        (home / "loop-mode.json").write_text(json.dumps(loop), encoding="utf-8")
    if checkpoint is not None:
        (home / "session_log.md").write_text(
            f"# log\n\n## Nap (turn 7) — {checkpoint}\nsummary\n", encoding="utf-8"
        )
    return home


def _run(tool, monkeypatch, home: Path, *extra: str, backup: str = "true", session: str = SESSION) -> int:
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return tool.main(["--session", session, "--backup-ok", backup, *extra])


def _compact_dir(home: Path) -> Path:
    return home / "compact"


def _nothing_written(home: Path) -> bool:
    d = _compact_dir(home)
    return (not d.exists()) or not list(d.iterdir())


LOOP = {
    "FIRST_ACTION": "do the work, then arm",
    "activated": "2026-10-11T13:05:27Z",
    "kind": "work",
    "topic": "a topic",
    "instructions": "do things",
    "state": "10-11 09:3x something",
    "cadence_seconds": 1800,
    "pacer": "scheduleWakeup",
    "loop_prompt": "<loop-wake>\nLoop continuation. Read the marker.",
}


# --------------------------------------------------------------------------- P1


def test_p1_happy_path(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path, loop=LOOP)
    assert _run(tool, monkeypatch, home) == 0
    d = _compact_dir(home)
    ctx = (d / "context.md").read_text(encoding="utf-8")
    # the focus list: an independent literal check AND byte-equality with the hook's builder
    assert f"- [{FOCUS_ID}] ({FOCUS_REASON}) {FOCUS_CLAIM}" in ctx
    builders = tool.load_builders(str(home))
    assert builders.focus_block(str(home)) in ctx
    assert builders.history_arc_block(str(home)) in ctx
    assert "shipped the thing" in ctx
    # the warm-briefing arc, verbatim
    assert ARC in ctx
    # the loop marker's fields
    assert "work" in ctx and "scheduleWakeup" in ctx and "1800" in ctx and "2026-10-11T13:05:27Z" in ctx
    # request.json: exactly the contract keys, matching digest, matching session
    req = json.loads((d / "request.json").read_text(encoding="utf-8"))
    assert set(req) == {"requested_at", "session_id", "loop", "context_sha256"}
    assert req["session_id"] == SESSION
    assert req["loop"] is True
    digest = hashlib.sha256(ctx.encode("utf-8")).hexdigest()
    assert req["context_sha256"] == digest
    assert (d / "context.sha256").read_text(encoding="utf-8").strip() == digest
    datetime.strptime(req["requested_at"], "%Y-%m-%dT%H:%M:%SZ")
    # the person's text above the anchor never enters the context
    assert "the person's words" not in ctx
    # no temp leftovers
    assert sorted(p.name for p in d.iterdir()) == ["context.md", "context.sha256", "request.json"]


def test_p1_request_is_written_last(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    order: list[str] = []
    real_replace = os.replace

    def spy(src, dst, *a, **k):
        order.append(Path(dst).name)
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", spy)
    assert _run(tool, monkeypatch, home) == 0
    assert order[-1] == "request.json"
    assert order.index("context.md") < order.index("request.json")
    assert order.index("context.sha256") < order.index("request.json")
    d = _compact_dir(home)
    assert (d / "request.json").stat().st_mtime_ns >= (d / "context.md").stat().st_mtime_ns


def test_p1_loop_flag_false_without_marker(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    assert _run(tool, monkeypatch, home) == 0
    req = json.loads((_compact_dir(home) / "request.json").read_text())
    assert req["loop"] is False


def test_p1_wake_line_from_caller(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path, loop=LOOP)
    assert _run(tool, monkeypatch, home, "--wake-armed-at", "09:41Z", "--wake-for", "10:11Z") == 0
    ctx = (_compact_dir(home) / "context.md").read_text(encoding="utf-8")
    assert "a ScheduleWakeup was armed at 09:41Z for 10:11Z" in ctx


# --------------------------------------------------------------------------- P2


def test_p2_backup_not_ok_refuses(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path)
    assert _run(tool, monkeypatch, home, backup="false") == 3
    assert _nothing_written(home)
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "backup" in err[0].lower()


@pytest.mark.parametrize(
    "kwargs, word",
    [
        ({"anchors": 0}, "anchor"),
        ({"anchors": 2}, "anchor"),
        ({"arc": None}, "arc"),
        ({"arc": "   \n"}, "arc"),
    ],
)
def test_p2_warm_briefing_rider_refuses(tool, monkeypatch, tmp_path, capsys, kwargs, word):
    home = _make_home(tmp_path, **kwargs)
    assert _run(tool, monkeypatch, home) == 3
    assert _nothing_written(home)
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and word in err[0].lower()


def test_p2_warm_briefing_older_than_checkpoint_refuses(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path, checkpoint="2030-01-01T00:00:00")
    assert _run(tool, monkeypatch, home) == 3
    assert _nothing_written(home)
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "older than the checkpoint" in err[0]


def test_p2_warm_briefing_missing_refuses(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path)
    (home / "warm-briefing.md").unlink()
    assert _run(tool, monkeypatch, home) == 3
    assert _nothing_written(home)
    assert "warm briefing" in capsys.readouterr().err.lower()


def test_p2_checkpoint_unknown_refuses(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path, checkpoint=None)
    assert _run(tool, monkeypatch, home) == 3
    assert _nothing_written(home)
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "checkpoint" in err[0]


def test_p2b_checkpoint_at_override_wins(tool, monkeypatch, tmp_path):
    # the log says "far future" (would refuse); the override says "long ago" (passes)
    home = _make_home(tmp_path, checkpoint="2030-01-01T00:00:00")
    assert _run(tool, monkeypatch, home, "--checkpoint-at", "2020-01-01T00:00:00Z") == 0
    # and the other way: log says long ago (passes), override says far future (refuses)
    home2 = _make_home(_sub(tmp_path, "b"))
    assert _run(tool, monkeypatch, home2, "--checkpoint-at", "2030-01-01T00:00:00Z") == 3
    assert _nothing_written(home2)


def test_p2b_checkpoint_taken_from_last_nap_header(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path, checkpoint=None)
    # an old header then a far-future last header: the LAST one decides (refuse)
    (home / "session_log.md").write_text(
        "## Nap (turn 1) — 2020-01-01T00:00:00\nx\n## Turn 2 — 2030-01-01T00:00:00\ny\n",
        encoding="utf-8",
    )
    assert _run(tool, monkeypatch, home) == 3
    # a last header long ago: accepted
    (home / "session_log.md").write_text(
        "## Nap (turn 1) — 2030-01-01T00:00:00\nx\n## Nap (turn 2) — 2020-01-01T00:00:00\ny\n",
        encoding="utf-8",
    )
    assert _run(tool, monkeypatch, home) == 0


def _sub(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.mkdir()
    return p


# --------------------------------------------------------------------------- P3


def test_p3_absent_pieces_are_named(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path, focus=False, history=False, loop=None)
    assert _run(tool, monkeypatch, home) == 0
    ctx = (_compact_dir(home) / "context.md").read_text(encoding="utf-8")
    assert "focus list: none found" in ctx
    assert "loop marker: none" in ctx
    # no history dir at all: the hook's own "no entries" block is used, or our named absence
    assert "history" in ctx.lower()


def test_p3_history_builder_returns_empty_is_named(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    real = tool.load_builders

    class Wrapped:
        def __init__(self, inner):
            self.focus_block = inner.focus_block
            self.history_arc_block = lambda h: ""

    monkeypatch.setattr(tool, "load_builders", lambda h: Wrapped(real(h)))
    assert _run(tool, monkeypatch, home) == 0
    ctx = (_compact_dir(home) / "context.md").read_text(encoding="utf-8")
    assert "history arc: unavailable" in ctx


def test_p3_unreadable_loop_marker_is_named(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    (home / "loop-mode.json").write_text("{not json", encoding="utf-8")
    assert _run(tool, monkeypatch, home) == 0
    ctx = (_compact_dir(home) / "context.md").read_text(encoding="utf-8")
    assert "loop marker: unreadable" in ctx


# --------------------------------------------------------------------------- P4


def test_p4_failure_between_context_and_request_leaves_no_request(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    real_replace = os.replace

    def failing(src, dst, *a, **k):
        if Path(dst).name == "request.json":
            raise OSError("injected: killed between context.md and request.json")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", failing)
    assert _run(tool, monkeypatch, home) == 4
    d = _compact_dir(home)
    assert not (d / "request.json").exists()
    # and no half-written temp file masquerading as the request
    assert not [p for p in d.iterdir() if p.name.startswith("request")]


# --------------------------------------------------------------------------- P5


def test_p5_older_request_is_replaced_not_left_beside(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    d = _compact_dir(home)
    d.mkdir()
    (d / "request.json").write_text(
        json.dumps({"requested_at": "2020-01-01T00:00:00Z", "session_id": "old", "loop": False, "context_sha256": "00"}),
        encoding="utf-8",
    )
    assert _run(tool, monkeypatch, home) == 0
    req = json.loads((d / "request.json").read_text())
    assert req["session_id"] == SESSION
    assert sorted(p.name for p in d.iterdir()) == ["context.md", "context.sha256", "request.json"]


def test_p5_older_request_is_removed_even_when_refused(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    d = _compact_dir(home)
    d.mkdir()
    (d / "request.json").write_text(json.dumps({"session_id": "old"}), encoding="utf-8")
    assert _run(tool, monkeypatch, home, backup="false") == 3
    assert not (d / "request.json").exists()


# --------------------------------------------------------------------------- P6


def test_p6_loading_builders_has_no_side_effects(tmp_path):
    home = _make_home(tmp_path)
    db = home / "knowledge.db"
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    before_names = sorted(p.name for p in home.iterdir())
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('cr', {str(TOOL_PATH)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"b = m.load_builders({str(home)!r})\n"
        f"b.focus_block({str(home)!r}); b.history_arc_block({str(home)!r})\n"
    )
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    # no -wal / -journal / new files appeared
    assert sorted(p.name for p in home.iterdir()) == before_names


# --------------------------------------------------------------------------- P7


def test_p7_size_cap_at_and_over(tool, monkeypatch, tmp_path):
    fixed = "2026-10-11T00:00:00Z"
    monkeypatch.setattr(tool, "_utc_now_iso", lambda: fixed)
    home = _make_home(tmp_path)
    assert _run(tool, monkeypatch, home) == 0
    size = len((_compact_dir(home) / "context.md").read_bytes())
    # exactly at the cap: written
    monkeypatch.setattr(tool, "MAX_CONTEXT_BYTES", size)
    assert _run(tool, monkeypatch, home) == 0
    assert (_compact_dir(home) / "request.json").exists()
    # one byte under the context's size: refused, exit 4, and nothing (not even the old request) is left
    monkeypatch.setattr(tool, "MAX_CONTEXT_BYTES", size - 1)
    assert _run(tool, monkeypatch, home) == 4
    assert not (_compact_dir(home) / "request.json").exists()


def test_p7_cap_counts_bytes_not_characters(tool, monkeypatch, tmp_path):
    fixed = "2026-10-11T00:00:00Z"
    monkeypatch.setattr(tool, "_utc_now_iso", lambda: fixed)
    home = _make_home(tmp_path, arc="é" * 40)
    assert _run(tool, monkeypatch, home) == 0
    raw = (_compact_dir(home) / "context.md").read_bytes()
    assert len(raw) > len(raw.decode("utf-8"))
    monkeypatch.setattr(tool, "MAX_CONTEXT_BYTES", len(raw.decode("utf-8")))  # chars would pass; bytes must not
    assert _run(tool, monkeypatch, home) == 4


def test_p7_default_cap_is_a_fixed_positive_number(tool):
    assert isinstance(tool.MAX_CONTEXT_BYTES, int) and 1_000 <= tool.MAX_CONTEXT_BYTES <= 200_000


# --------------------------------------------------------------------------- P8


def test_p8_no_loop_wake_text_anywhere(tool, monkeypatch, tmp_path):
    arc = "arc mentions &lt;loop-wake&gt; and a Loop-Wake and LOOP-WAKE here"
    home = _make_home(tmp_path, loop=LOOP, arc=arc)
    assert _run(tool, monkeypatch, home) == 0
    ctx = (_compact_dir(home) / "context.md").read_text(encoding="utf-8")
    low = ctx.lower()
    assert "loop-wake" not in low
    assert "loop_wake" not in low and "loopwake" not in low
    assert "<loop-wake>" not in low and "&lt;loop-wake&gt;" not in low
    # the marker's prompt text is excluded, its structured fields are kept
    assert "Loop continuation" not in ctx
    assert "scheduleWakeup" in ctx
    # the rest of the arc survives the scrub
    assert "arc mentions" in ctx


# --------------------------------------------------------------------------- P9


def test_p9_hook_found_in_deployed_and_source_layouts(tool, tmp_path):
    name = tool.HOOK_NAME
    deployed = tmp_path / "plugin"
    (deployed / "tools").mkdir(parents=True)
    (deployed / "hooks").mkdir()
    (deployed / "hooks" / name).write_text("# hook\n")
    assert tool.find_hook(deployed / "tools") == deployed / "hooks" / name
    source = tmp_path / "src-engram"
    (source / "tools").mkdir(parents=True)
    (source / "hooks" / "claude").mkdir(parents=True)
    (source / "hooks" / "claude" / name).write_text("# hook\n")
    assert tool.find_hook(source / "tools") == source / "hooks" / "claude" / name
    empty = tmp_path / "empty"
    (empty / "tools").mkdir(parents=True)
    assert tool.find_hook(empty / "tools") is None


def test_p9_missing_hook_is_exit_4_nothing_written(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path)
    monkeypatch.setattr(tool, "find_hook", lambda d: None)
    assert _run(tool, monkeypatch, home) == 4
    assert not (_compact_dir(home) / "request.json").exists()
    assert "hook" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- P10


def test_p10_symlink_entry_point_and_exit_codes(tmp_path):
    assert LINK_PATH.is_symlink()
    assert LINK_PATH.resolve() == TOOL_PATH
    home = _make_home(tmp_path)
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    ok = subprocess.run(
        [sys.executable, str(LINK_PATH), "--session", SESSION, "--backup-ok", "true"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert ok.returncode == 0, ok.stderr
    assert (home / "compact" / "request.json").exists()
    refused = subprocess.run(
        [sys.executable, str(LINK_PATH), "--session", SESSION, "--backup-ok", "false"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert refused.returncode == 3
    assert not (home / "compact" / "request.json").exists()


def test_p10_bad_backup_value_is_a_usage_error(tmp_path):
    home = _make_home(tmp_path)
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    bad = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--session", SESSION, "--backup-ok", "maybe"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert bad.returncode == 2
    assert "invalid choice" in bad.stderr
    assert not (home / "compact").exists()


def test_p10_bad_args_still_clear_an_older_request(tmp_path):
    home = _make_home(tmp_path)
    d = home / "compact"
    d.mkdir()
    (d / "request.json").write_text(json.dumps({"session_id": "old"}), encoding="utf-8")
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    bad = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--session", SESSION, "--backup-ok", "maybe"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert bad.returncode == 2
    assert not (d / "request.json").exists()
    # and a call missing a required argument does the same
    (d / "request.json").write_text(json.dumps({"session_id": "old"}), encoding="utf-8")
    missing = subprocess.run([sys.executable, str(TOOL_PATH)], capture_output=True, text=True, env=env, timeout=60)
    assert missing.returncode == 2
    assert not (d / "request.json").exists()


# --------------------------------------------------------------------------- P11 --clear


def _seed_compact_dir(home: Path) -> Path:
    d = home / "compact"
    d.mkdir()
    for name in ("request.json", "inflight", "context.md", "context.sha256"):
        (d / name).write_text("x", encoding="utf-8")
    return d


def test_p11_clear_removes_a_live_request_and_its_files(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    d = _seed_compact_dir(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert tool.main(["--clear"]) == 0
    assert list(d.iterdir()) == []


def test_p11_clear_with_nothing_present_is_exit_0(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert tool.main(["--clear"]) == 0
    assert _nothing_written(home)


def test_p11_clear_never_writes_a_request_and_needs_no_other_args(tool, monkeypatch, tmp_path, capsys):
    # no riders: a home whose warm briefing would FAIL them still clears with exit 0
    home = _make_home(tmp_path, anchors=0, checkpoint=None)
    d = _seed_compact_dir(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert tool.main(["--clear", "--session", SESSION]) == 0
    assert not (d / "request.json").exists()
    assert not (d / "context.md").exists()
    assert capsys.readouterr().err == ""


def test_p11_clear_cli_exit_code(tmp_path):
    home = _make_home(tmp_path)
    d = _seed_compact_dir(home)
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    out = subprocess.run([sys.executable, str(LINK_PATH), "--clear"], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert list(d.iterdir()) == []


def _unlink_failing_for(monkeypatch, name: str) -> None:
    real_unlink = Path.unlink

    def fake_unlink(self, *args, **kwargs):
        if self.name == name:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)


def test_p11_clear_exit_4_when_the_old_request_cannot_be_removed(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path)
    d = _seed_compact_dir(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    _unlink_failing_for(monkeypatch, "request.json")
    assert tool.main(["--clear"]) == 4
    assert "cannot remove the old request" in capsys.readouterr().err
    assert (d / "request.json").exists()


def test_p11_clear_exit_4_when_a_later_file_cannot_be_removed(tool, monkeypatch, tmp_path, capsys):
    home = _make_home(tmp_path)
    d = _seed_compact_dir(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    _unlink_failing_for(monkeypatch, "context.md")
    assert tool.main(["--clear"]) == 4
    err = capsys.readouterr().err
    assert "context.md" in err
    assert "the old request" not in err
    assert not (d / "request.json").exists()


def test_p11_clear_ignores_session(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    d = _seed_compact_dir(home)
    (d / "request.json").write_text(json.dumps({"session_id": "sess-A"}), encoding="utf-8")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert tool.main(["--clear", "--session", "sess-B"]) == 0
    assert not (d / "request.json").exists()


def test_p11_any_run_removes_another_sessions_request(tool, monkeypatch, tmp_path):
    home = _make_home(tmp_path, anchors=0)  # the rider fails, so the run is refused
    d = _seed_compact_dir(home)
    (d / "request.json").write_text(json.dumps({"session_id": "sess-A"}), encoding="utf-8")
    assert _run(tool, monkeypatch, home, session="sess-B") == 3
    assert not (d / "request.json").exists()


def test_p11_any_run_exit_4_when_the_old_request_cannot_be_removed(tool, monkeypatch, tmp_path, capsys):
    # the first removal runs on EVERY invocation, not only --clear: a normal run fails the same way
    home = _make_home(tmp_path)
    d = _seed_compact_dir(home)
    _unlink_failing_for(monkeypatch, "request.json")
    assert _run(tool, monkeypatch, home) == 4
    assert "cannot remove the old request" in capsys.readouterr().err
    assert (d / "request.json").read_text(encoding="utf-8") == "x"  # the old request, untouched
