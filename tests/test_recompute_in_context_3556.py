"""Contract tests for tools/probes/mods-recall/recompute_in_context.py (engram-alpha#3556 Half A).

Contract-first (CLAUDE.md "Contract-first tests"): written BEFORE the tool existed.
Every expected value is hand-computed in the comment beside it from the literals below.

Time model: every fixture time is T0 + N seconds (ms precision). A transcript line is
`tl(N, ...)`, a ledger line fires at `ts = T0 + N*1000`. B marks a compaction boundary
(a `type:"user"` line with `isCompactSummary: true`).

Definition under test (Kepler's ruling + the brief's D1-D3): for ledger time t and candidate x,
the boundary is the LAST isCompactSummary line with timestamp < t; the window is the lines with
B.timestamp < timestamp < t (strict). First match wins: summary (x in B's own message), shown (x in
a window line), created (created_at in [B.ts, t)), focused (the hook's own why), none.
No boundary before t -> the SAME computation from the transcript text, with the window
[first transcript line timestamp, t) and no `summary` step (Kepler's real-run defect, PR #3567:
copying the hook flag left every pre-first-boundary candidate `unknown` on a pre-#3554 seat).
The hook's own flag always rides beside it (`in_context_hook`, `in_context_hook_why`).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "probes" / "mods-recall" / "recompute_in_context.py"
RATING_TS = ROOT / "tools" / "probes" / "mods-recall" / "hooks" / "rating.ts"

T0 = 1_790_000_000_000  # ms since epoch (2026-09-21, UTC)


@pytest.fixture()
def tool():
    spec = importlib.util.spec_from_file_location("recompute_in_context_3556", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


@pytest.fixture()
def strict_z(tool, monkeypatch):
    """Make the tool's `datetime.fromisoformat` behave like Python < 3.11: a trailing Z is rejected."""
    real = tool.datetime

    class Old(real):
        @classmethod
        def fromisoformat(cls, value):
            if value.endswith(("Z", "z")):
                raise ValueError(f"Invalid isoformat string: {value!r}")
            return real.fromisoformat(value)

    monkeypatch.setattr(tool, "datetime", Old)
    return tool


# ---------------------------------------------------------------- builders
def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def naive(ms: int) -> str:
    """created_at as the hook writes it when it has no zone: UTC wall clock, no suffix."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def at(sec: float) -> int:
    return T0 + int(sec * 1000)


def tl(sec, text, type_="user", **extra):
    d = {"type": type_, "timestamp": iso(at(sec)), "message": {"content": text}}
    d.update(extra)
    return d


def boundary(sec, text="This session is being continued from a previous conversation. Summary: nothing."):
    return tl(sec, text, isCompactSummary=True)


def cand(cid, created_at="2020-01-01T00:00:00Z", hook=False, why=None):
    return {"id": cid, "rank": 1, "created_at": created_at, "in_context": hook, "in_context_why": why}


def lline(sec, cands, route="prompt_surface", fire_key="fk"):
    return {"ts": at(sec), "route": route, "session_id": "s1", "fire_key": fire_key,
            "candidates": cands, "shown": [], "cut": {}}


def write_jsonl(path: Path, rows, raw_tail: str = "") -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + raw_tail)


def setup(tmp_path, transcript, ledgers, raw_tail=None):
    """ledgers: {route: [ledger lines]}; raw_tail: {route: str appended verbatim}."""
    tr = tmp_path / "t.jsonl"
    write_jsonl(tr, transcript)
    routes = tmp_path / "routes"
    routes.mkdir()
    for route, rows in ledgers.items():
        write_jsonl(routes / f"{route}.jsonl", rows, (raw_tail or {}).get(route, ""))
    return tr, routes, tmp_path / "out"


def run(tool, tmp_path, transcript, ledgers, raw_tail=None):
    tr, routes, out = setup(tmp_path, transcript, ledgers, raw_tail)
    rc = tool.main([str(tr), str(routes), "--out", str(out)])
    return rc, out


def rows(out: Path, route="prompt_surface"):
    return [json.loads(l) for l in (out / f"{route}.in_context_offline.jsonl").read_text().splitlines()]


def one(tool, tmp_path, transcript, cands, t=10, route="prompt_surface"):
    """Run one ledger line at second t; return {id: (offline, why)}."""
    rc, out = run(tool, tmp_path, transcript, {route: [lline(t, cands, route)]})
    assert rc == 0
    return {c["id"]: (c["in_context_offline"], c["in_context_offline_why"]) for c in rows(out, route)[0]["candidates"]}


# ---------------------------------------------------------------- cells
def test_cell1_pre_compaction_shown_is_false_after_boundary(tool, tmp_path):
    # ob_0001 appears at s1 (before B at s2). Ledger t=s10. Window = (s2, s10) holds only filler.
    # Hook said true/'shown' (it never resets); offline must say false/'none'. created_at 2020 < B.
    tr = [tl(1, "recall block ob_0001"), boundary(2), tl(3, "filler only")]
    got = one(tool, tmp_path, tr, [cand("ob_0001", hook=True, why="shown")])
    assert got == {"ob_0001": (False, "none")}


def test_cell2_summary_carried(tool, tmp_path):
    # ob_0002 only in B's own message -> true / summary. Also in a window line -> summary still wins (first match).
    tr = [boundary(2, "This session is being continued ... carries ob_0002 and ob_0012"), tl(3, "later ob_0012")]
    got = one(tool, tmp_path, tr, [cand("ob_0002"), cand("ob_0012")])
    assert got == {"ob_0002": (True, "summary"), "ob_0012": (True, "summary")}


def test_cell3_post_compaction_shown(tool, tmp_path):
    # ob_0003 in a line at s5, B at s2, t=s10 -> in window -> true / shown.
    tr = [boundary(2), tl(5, "injected recall ob_0003")]
    got = one(tool, tmp_path, tr, [cand("ob_0003")])
    assert got == {"ob_0003": (True, "shown")}


def test_cell4_created_before_vs_after_boundary_naive_is_utc(tool, tmp_path, monkeypatch):
    # B at s2, t=s10. ob_0004a created s1 (< B) -> false/none. ob_0004b created s5 (in [B,t)) -> true/created.
    # created_at is NAIVE (UTC wall clock). TZ is set to Los Angeles (UTC-7 in September): read as local,
    # s5 would be 7 h later than t -> not < t -> false. Read as UTC -> true. ob_0004c created s11 (>= t) -> false.
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        tr = [boundary(2), tl(3, "filler")]
        got = one(tool, tmp_path, tr, [
            cand("ob_0004a", created_at=naive(at(1))),
            cand("ob_0004b", created_at=naive(at(5))),
            cand("ob_0004c", created_at=naive(at(11))),
            cand("ob_0004d", created_at=iso(at(5))),  # Z suffix form of the same instant
        ])
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
    assert got == {"ob_0004a": (False, "none"), "ob_0004b": (True, "created"),
                   "ob_0004c": (False, "none"), "ob_0004d": (True, "created")}


def test_cell5_zero_boundaries_offline_computed_from_text(tool, tmp_path):
    # EDITED by F1 (was: offline == hook). No isCompactSummary line anywhere: window = [first transcript
    # line (s1), t=s10). created_at default is 2020, i.e. before the transcript start -> never 'created'.
    #   ob_0050 text at s1 (the FIRST line, inclusive) -> true/shown            (hook true/shown: agree)
    #   ob_0051 no text, created 2020 -> false/none     (was copied hook false/None; hook kept beside)
    #   ob_0052 no text, created 2020 -> false/none     (was copied null/null)
    #   ob_0053 no text -> false/none                   (was copied true/created; hook kept beside)
    #   ob_0054 no text, hook why 'focused' -> true/focused
    tr = [tl(1, "ob_0050 sits here"), tl(2, "other")]
    cs = [cand("ob_0050", hook=True, why="shown"), cand("ob_0051", hook=False, why=None),
          cand("ob_0052", hook=None, why=None), cand("ob_0053", hook=True, why="created"),
          cand("ob_0054", hook=True, why="focused")]
    got = one(tool, tmp_path, tr, cs)
    assert got == {"ob_0050": (True, "shown"), "ob_0051": (False, "none"), "ob_0052": (False, "none"),
                   "ob_0053": (False, "none"), "ob_0054": (True, "focused")}


def test_cell5b_zero_boundaries_sidecar_row_shape(tool, tmp_path):
    # EDITED by F1: the transcript text 'x' does not carry ob_0050, so offline is false/none while the
    # hook's own true/shown stays beside it.
    tr = [tl(1, "x")]
    rc, out = run(tool, tmp_path, tr, {"prompt_surface": [lline(10, [cand("ob_0050", hook=True, why="shown")], fire_key="K")]})
    r = rows(out)[0]
    assert rc == 0
    assert (r["ts"], r["route"], r["fire_key"], r["boundary_index"], r["boundary_ts"]) == (at(10), "prompt_surface", "K", None, None)
    assert r["candidates"] == [{"id": "ob_0050", "in_context_offline": False, "in_context_offline_why": "none",
                                "in_context_hook": True, "in_context_hook_why": "shown"}]


def test_cell6_two_boundaries_only_last_before_t_counts(tool, tmp_path):
    # B1 at s2 carries ob_0006; B2 at s6 does not. Ledger A at s4 (before B2) uses B1 -> true/summary.
    # Ledger C at s10 uses B2 -> ob_0006 false/none. boundary_index 0 for A, 1 for C.
    tr = [boundary(2, "continued ... ob_0006"), tl(3, "x"), boundary(6, "continued ... nothing"), tl(7, "y")]
    rc, out = run(tool, tmp_path, tr, {"prompt_surface": [lline(4, [cand("ob_0006")]), lline(10, [cand("ob_0006")])]})
    a, c = rows(out)
    assert rc == 0
    assert (a["boundary_index"], a["boundary_ts"]) == (0, iso(at(2)))
    assert [(x["in_context_offline"], x["in_context_offline_why"]) for x in a["candidates"]] == [(True, "summary")]
    assert (c["boundary_index"], c["boundary_ts"]) == (1, iso(at(6)))
    assert [(x["in_context_offline"], x["in_context_offline_why"]) for x in c["candidates"]] == [(False, "none")]


def test_cell7_strict_before_t(tool, tmp_path):
    # ob_0007 only in a line at exactly t (s10) -> not in W -> false. ob_0008 in a line 1 ms before t -> true.
    tr = [boundary(2), tl(10, "at t ob_0007"), tl(9.999, "just before ob_0008")]
    got = one(tool, tmp_path, tr, [cand("ob_0007"), cand("ob_0008")], t=10)
    assert got == {"ob_0007": (False, "none"), "ob_0008": (True, "shown")}


def test_cell7b_boundary_at_exactly_t_is_not_before_t(tool, tmp_path):
    # A boundary stamped exactly t does not count (timestamp < t is strict): no boundary -> offline == hook.
    tr = [tl(1, "ob_0009 here"), boundary(10)]
    got = one(tool, tmp_path, tr, [cand("ob_0009", hook=True, why="shown")], t=10)
    assert got == {"ob_0009": (True, "shown")}


def test_cell8_rating_request_lines_skipped(tool, tmp_path):
    marker = tool.REQUEST_MARKER
    # ob_0009 only in a rating-request line -> false. ob_0011 in the request line AND a plain line -> true (control).
    tr = [boundary(2),
          tl(4, f"{marker}abcd1234] Rate what thinking-recall surfaced: ob_0009 ob_0011"),
          tl(5, "plain line with ob_0011")]
    got = one(tool, tmp_path, tr, [cand("ob_0009"), cand("ob_0011")])
    assert got == {"ob_0009": (False, "none"), "ob_0011": (True, "shown")}


def test_cell8b_marker_literal_pinned_to_rating_ts(tool):
    m = re.search(r"export const REQUEST_MARKER = '([^']*)'", RATING_TS.read_text())
    assert m, "REQUEST_MARKER declaration not found in rating.ts"
    assert tool.REQUEST_MARKER == m.group(1)


def test_cell8c_summary_quoting_the_marker_still_carries_its_ids(tool, tmp_path):
    # A compaction summary of a probe session may quote the marker. It is not a rating request:
    # its ids are what post-compaction context holds. (Judgment call recorded in the handoff.)
    tr = [boundary(2, f"continued ... the line {tool.REQUEST_MARKER}ab] listed ob_0013")]
    got = one(tool, tmp_path, tr, [cand("ob_0013")])
    assert got == {"ob_0013": (True, "summary")}


def test_cell9_sidechain_and_excluded_types_skipped(tool, tmp_path):
    tr = [boundary(2),
          tl(3, "sidechain ob_0014", isSidechain=True),
          tl(4, "queue ob_0015", type_="queue-operation"),
          tl(4.1, "title ob_0016", type_="custom-title"),
          tl(4.2, "sys ob_0017", type_="system"),
          tl(5, "plain ob_0018")]
    got = one(tool, tmp_path, tr, [cand(f"ob_001{i}") for i in range(4, 9)])
    assert got == {"ob_0014": (False, "none"), "ob_0015": (False, "none"), "ob_0016": (False, "none"),
                   "ob_0017": (False, "none"), "ob_0018": (True, "shown")}


def test_cell10_hook_success_stdout_decoded_and_additional_context_list(tool, tmp_path):
    # stdout is a JSON STRING. json.dumps (ensure_ascii) writes the bullet as the 6 chars backslash-u-2-0-2-2,
    # so the raw text reads "...•ob_0020": the char before "ob" is the digit 2 -> no word boundary -> the raw
    # string hides the id from the regex. Only decoding finds it.
    stdout = json.dumps({"hookSpecificOutput": {"additionalContext": "focus list •ob_0020 and ob_0022"}})
    assert "\\u2022ob_0020" in stdout
    tr = [boundary(2),
          tl(3, None, type_="attachment", attachment={"type": "hook_success", "hookEvent": "SessionStart", "stdout": stdout}),
          tl(4, None, type_="attachment", attachment={"type": "hook_additional_context", "content": ["recall ob_0021", "second"]})]
    got = one(tool, tmp_path, tr, [cand("ob_0020"), cand("ob_0021"), cand("ob_0022")])
    assert got == {"ob_0020": (True, "shown"), "ob_0021": (True, "shown"), "ob_0022": (True, "shown")}


def stub_text(path, size="23.3KB"):
    return f"<persisted-output>\nOutput too large ({size}). Full output saved to: {path}\n\nPreview (first 2KB):\nnothing useful\n</persisted-output>"


def stub_run(tool, tmp_path, transcript, cands, extra_args=()):
    tr, routes, out = setup(tmp_path, transcript, {"prompt_surface": [lline(10, cands)]})
    rc = tool.main([str(tr), str(routes), "--out", str(out), *extra_args])
    s = json.loads((out / "summary.json").read_text()) if (out / "summary.json").exists() else None
    got = {c["id"]: (c["in_context_offline"], c["in_context_offline_why"]) for c in rows(out)[0]["candidates"]} if s else None
    return rc, got, s


def test_cell11_stub_without_file_ids_not_seen(tool, tmp_path):
    # The stub text itself carries no id; the saved file is absent -> ob_0030 not seen, 1 stub missing, 0 read.
    gone = tmp_path / "tool-results" / "gone.txt"
    rc, got, s = stub_run(tool, tmp_path, [boundary(2), tl(3, stub_text(gone)), tl(5, "mentions Output too large only")],
                          [cand("ob_0030")])
    assert rc == 0
    assert got == {"ob_0030": (False, "none")}
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (0, 1)
    assert "persisted_output_stubs_seen" not in s


def test_cell15_stub_file_in_allowed_root_counts_as_persisted(tool, tmp_path):
    # Transcript dir (tmp_path) is an allowed root. The file holds ob_0090 -> true/persisted, stubs_read 1.
    f = tmp_path / "tool-results" / "r.txt"
    f.parent.mkdir()
    f.write_text("big output with ob_0090 inside")
    rc, got, s = stub_run(tool, tmp_path, [boundary(2), tl(3, stub_text(f))], [cand("ob_0090"), cand("ob_0091")])
    assert rc == 0
    assert got == {"ob_0090": (True, "persisted"), "ob_0091": (False, "none")}
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (1, 0)
    assert s["by_why"] == {"persisted": 1, "none": 1}


def test_cell15b_explicit_persisted_root(tool, tmp_path):
    # A root outside the transcript dir is refused by default and allowed by --persisted-root.
    d = tmp_path / "work"
    d.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    f = other / "r.txt"
    f.write_text("ob_0092")
    rc, got, s = stub_run(tool, d, [boundary(2), tl(3, stub_text(f))], [cand("ob_0092")])
    assert got == {"ob_0092": (False, "none")} and s["persisted_output_stubs_missing"] == 1
    (d / "w2").mkdir()
    rc, got, s = stub_run(tool, d / "w2", [boundary(2), tl(3, stub_text(f))], [cand("ob_0092")],
                          extra_args=("--persisted-root", str(other)))
    assert got == {"ob_0092": (True, "persisted")} and s["persisted_output_stubs_read"] == 1


def test_cell17_stub_path_outside_root_refused(tool, tmp_path, capsys):
    # /etc/passwd is outside every allowed root; a symlink INSIDE the root pointing outside resolves outside.
    # Nothing is read; both count missing; stderr names each path. The outside file holds an id: must not be seen.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("ob_0095")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link.txt"
    link.symlink_to(secret)
    rc, got, s = stub_run(tool, root, [boundary(2), tl(3, stub_text("/etc/passwd")), tl(4, stub_text(link))],
                          [cand("ob_0095")])
    err = capsys.readouterr().err
    assert rc == 0
    assert got == {"ob_0095": (False, "none")}
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (0, 2)
    assert "/etc/passwd" in err and "link.txt" in err


def test_cell17b_non_regular_and_oversize_files_are_missing(tool, tmp_path):
    d = tmp_path / "tool-results"
    d.mkdir()
    big = d / "big.txt"
    big.write_text("ob_0096 " + "x" * (5 * 1024 * 1024))  # 5 MiB + 8 bytes > cap
    ok = d / "ok.txt"
    ok.write_text("ob_0097")
    rc, got, s = stub_run(tool, tmp_path, [boundary(2), tl(3, stub_text(big)), tl(4, stub_text(d)), tl(5, stub_text(ok))],
                          [cand("ob_0096"), cand("ob_0097")])
    assert got == {"ob_0096": (False, "none"), "ob_0097": (True, "persisted")}
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (1, 2)


def test_cell18_plain_hit_beats_persisted(tool, tmp_path):
    # ob_0098 in a stub file AND a plain window line -> shown. ob_0099 in the summary and a stub file -> summary.
    f = tmp_path / "r.txt"
    f.write_text("ob_0098 ob_0099")
    rc, got, s = stub_run(tool, tmp_path, [boundary(2, "continued ob_0099"), tl(3, stub_text(f)), tl(4, "plain ob_0098")],
                          [cand("ob_0098"), cand("ob_0099")])
    assert got == {"ob_0098": (True, "shown"), "ob_0099": (True, "summary")}


def test_cell12_truncated_last_ledger_line_skipped_and_counted(tool, tmp_path):
    tr = [boundary(2), tl(3, "ob_0031")]
    rc, out = run(tool, tmp_path, tr, {"prompt_surface": [lline(10, [cand("ob_0031")])]},
                  raw_tail={"prompt_surface": '{"ts": 17900000'})
    s = json.loads((out / "summary.json").read_text())
    assert rc == 0
    assert len(rows(out)) == 1
    assert s["ledger_lines"] == 1 and s["truncated_ledger_lines"] == 1


def test_cell12b_unparsable_middle_ledger_line_is_an_error_naming_the_line(tool, tmp_path, capsys):
    tr, routes, out = setup(tmp_path, [boundary(2)], {})
    good = json.dumps(lline(10, [cand("ob_0032")]))
    (routes / "prompt_surface.jsonl").write_text(good + "\n{not json\n" + good + "\n")
    rc = tool.main([str(tr), str(routes), "--out", str(out)])
    err = capsys.readouterr().err
    assert rc != 0
    assert "prompt_surface.jsonl" in err and "line 2" in err
    assert not out.exists()


def test_cell12c_ledger_line_missing_ts_or_candidates_is_an_error(tool, tmp_path, capsys):
    tr, routes, out = setup(tmp_path, [boundary(2)], {"prompt_surface": [{"route": "prompt_surface", "candidates": []}]})
    rc = tool.main([str(tr), str(routes), "--out", str(out)])
    err = capsys.readouterr().err
    assert rc != 0 and "prompt_surface.jsonl" in err and "line 1" in err
    assert not out.exists()


def test_cell13_cli_errors_name_the_file_and_write_nothing(tool, tmp_path, capsys):
    good_t = [boundary(2)]
    good_l = {"prompt_surface": [lline(10, [cand("ob_0040")])]}

    # unreadable transcript
    tr, routes, out = setup(tmp_path / "a", good_t, good_l) if (tmp_path / "a").mkdir() is None else None
    missing = tmp_path / "a" / "nope.jsonl"
    assert tool.main([str(missing), str(routes), "--out", str(out)]) != 0
    assert "nope.jsonl" in capsys.readouterr().err
    assert not out.exists()

    # zero parsable conversation lines
    (tmp_path / "b").mkdir()
    tr, routes, out = setup(tmp_path / "b", [], good_l)
    tr.write_text("not json\n{\"type\": \"mode\"}\n")
    assert tool.main([str(tr), str(routes), "--out", str(out)]) != 0
    assert "t.jsonl" in capsys.readouterr().err
    assert not out.exists()

    # no ledger lines
    (tmp_path / "c").mkdir()
    tr, routes, out = setup(tmp_path / "c", good_t, {"prompt_surface": []})
    assert tool.main([str(tr), str(routes), "--out", str(out)]) != 0
    assert "routes" in capsys.readouterr().err
    assert not out.exists()

    # --out exists and is non-empty: refused, existing content untouched
    (tmp_path / "d").mkdir()
    tr, routes, out = setup(tmp_path / "d", good_t, good_l)
    out.mkdir()
    (out / "keep.txt").write_text("keep")
    assert tool.main([str(tr), str(routes), "--out", str(out)]) != 0
    assert str(out) in capsys.readouterr().err
    assert [p.name for p in out.iterdir()] == ["keep.txt"]

    # --out inside the routes dir: refused
    (tmp_path / "e").mkdir()
    tr, routes, out = setup(tmp_path / "e", good_t, good_l)
    inside = routes / "sidecar"
    assert tool.main([str(tr), str(routes), "--out", str(inside)]) != 0
    assert not inside.exists()


def test_cell13b_inputs_byte_identical_and_modes(tool, tmp_path):
    tr, routes, out = setup(tmp_path, [tl(1, "ob_0041"), boundary(2), tl(3, "ob_0042")],
                            {"prompt_surface": [lline(10, [cand("ob_0042")])],
                             "tool_recall": [lline(11, [cand("ob_0041", hook=True, why="shown")], "tool_recall")]})
    before = {p: p.read_bytes() for p in [tr, *routes.iterdir()]}
    names_before = sorted(p.name for p in routes.iterdir())
    assert tool.main([str(tr), str(routes), "--out", str(out)]) == 0
    assert {p: p.read_bytes() for p in before} == before
    assert sorted(p.name for p in routes.iterdir()) == names_before
    assert (out.stat().st_mode & 0o777) == 0o700
    produced = sorted(p.name for p in out.iterdir())
    assert produced == ["prompt_surface.in_context_offline.jsonl", "summary.json", "tool_recall.in_context_offline.jsonl"]
    for p in out.iterdir():
        assert (p.stat().st_mode & 0o777) == 0o600


def test_cell14_focused_kept_when_no_text_hit_after_boundary(tool, tmp_path):
    # B at s2; created long before; no text hit. hook why 'focused' -> true/focused (live focus set is unrecoverable
    # offline, so the hook's own value is kept). hook why 'shown' with no text hit -> false/none (pre-compaction shown).
    # A text hit beats focused (ob_0062 -> shown).
    tr = [boundary(2), tl(3, "ob_0062")]
    got = one(tool, tmp_path, tr, [cand("ob_0060", hook=True, why="focused"),
                                   cand("ob_0061", hook=True, why="shown"),
                                   cand("ob_0062", hook=True, why="focused")])
    assert got == {"ob_0060": (True, "focused"), "ob_0061": (False, "none"), "ob_0062": (True, "shown")}


def test_cell19_unparsable_created_at_is_null_not_false(tool, tmp_path):
    # B exists, no text hit, created_at unparsable, hook not focused -> unknown: null / null, never false.
    tr = [boundary(2), tl(3, "filler")]
    got = one(tool, tmp_path, tr, [cand("ob_0070", created_at="garbage"), cand("ob_0071", created_at=None),
                                   cand("ob_0072", created_at="garbage", hook=True, why="focused")])
    assert got == {"ob_0070": (None, None), "ob_0071": (None, None), "ob_0072": (True, "focused")}


def test_cell20_summary_json_counts(tool, tmp_path):
    # transcript: B1 s2 (summary has ob_0080), B2 s6, junk line. Two ledger lines (s4, s10), 2 candidates each.
    # s4 (B1): ob_0080 summary, ob_0081 none ; s10 (B2): ob_0080 none, ob_0081 shown (line at s7).
    # hook: ob_0080 true/shown everywhere, ob_0081 false/None everywhere.
    # disagree (offline != hook, both non-null): s4: ob_0080 True==True no; ob_0081 False==False no;
    #   s10: ob_0080 False != True yes; ob_0081 True != False yes -> 2.
    tr_rows = [boundary(2, "continued ob_0080"), boundary(6, "continued"), tl(7, "ob_0081")]
    tr, routes, out = setup(tmp_path, tr_rows, {"prompt_surface": [
        lline(4, [cand("ob_0080", hook=True, why="shown"), cand("ob_0081")]),
        lline(10, [cand("ob_0080", hook=True, why="shown"), cand("ob_0081")])]})
    with open(tr, "a") as f:
        f.write("this is not json\n")
    assert tool.main([str(tr), str(routes), "--out", str(out)]) == 0
    s = json.loads((out / "summary.json").read_text())
    assert s["transcript"] == str(tr)
    assert (s["ledger_lines"], s["boundaries"], s["candidates"]) == (2, 2, 4)
    assert s["by_why"] == {"summary": 1, "none": 2, "shown": 1}
    assert s["disagree_with_hook"] == 2
    assert s["unparsable_transcript_lines"] == 1
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (0, 0)


# ================================================================ review round 1 + real-run defect (PR #3567)
def test_cell21_row_before_the_first_boundary_is_computed_from_the_start_of_the_transcript(tool, tmp_path):
    # Kepler's real-run defect: a session WITH a boundary (B at s5) and a ledger row BEFORE it (t=s3).
    # No boundary precedes t -> window [first line s1, s3), no 'summary' step. Hook flags are all false/None.
    #   ob_0110 text at s1 (first line, inclusive)              -> true/shown
    #   ob_0111 text at s2                                      -> true/shown
    #   ob_0112 only in B's summary at s5 (after t)             -> false/none  (summary never applies here)
    #   ob_0113 created s2.5 in [s1, s3)                        -> true/created
    #   ob_0114 created exactly t (s3)                          -> false/none  (end is exclusive)
    #   ob_0115 created exactly s1 (transcript start)           -> true/created (start is inclusive)
    #   ob_0116 created s0.5 (before the transcript)            -> false/none
    #   ob_0117 created 'garbage', no hit, hook not focused     -> null/null   (genuinely unknowable)
    #   ob_0118 created 'garbage', hook why 'focused'           -> true/focused
    #   ob_0119 text only at exactly t (s3)                     -> false/none  (strict)
    tr = [tl(1, "early ob_0110"), tl(2, "ob_0111 too"), tl(3, "at t ob_0119"),
          boundary(5, "continued ... ob_0112"), tl(6, "post")]
    cs = [cand("ob_0110"), cand("ob_0111"), cand("ob_0112"),
          cand("ob_0113", created_at=naive(at(2.5))), cand("ob_0114", created_at=naive(at(3))),
          cand("ob_0115", created_at=naive(at(1))), cand("ob_0116", created_at=naive(at(0.5))),
          cand("ob_0117", created_at="garbage"), cand("ob_0118", created_at="garbage", hook=True, why="focused"),
          cand("ob_0119")]
    # a second row AFTER the boundary: ob_0110 (pre-boundary text) no longer counts, ob_0112 is summary.
    rc, out = run(tool, tmp_path, tr, {"prompt_surface": [lline(3, cs), lline(7, [cand("ob_0110"), cand("ob_0112")])]})
    assert rc == 0
    early, late = rows(out)
    assert (early["boundary_index"], early["boundary_ts"]) == (None, None)
    got = {c["id"]: (c["in_context_offline"], c["in_context_offline_why"]) for c in early["candidates"]}
    assert got == {"ob_0110": (True, "shown"), "ob_0111": (True, "shown"), "ob_0112": (False, "none"),
                   "ob_0113": (True, "created"), "ob_0114": (False, "none"), "ob_0115": (True, "created"),
                   "ob_0116": (False, "none"), "ob_0117": (None, None), "ob_0118": (True, "focused"),
                   "ob_0119": (False, "none")}
    assert late["boundary_index"] == 0
    assert [(c["in_context_offline"], c["in_context_offline_why"]) for c in late["candidates"]] == \
        [(False, "none"), (True, "summary")]


def test_cell21b_pre_boundary_window_sees_persisted_output(tool, tmp_path):
    # Order without summary: shown -> persisted -> created. ob_0120 only in a stub file a pre-boundary line
    # points at -> true/persisted; ob_0121 in the file AND in plain text -> shown.
    f = tmp_path / "r.txt"
    f.write_text("ob_0120 ob_0121")
    rc, got, s = stub_run(tool, tmp_path, [tl(1, stub_text(f)), tl(2, "plain ob_0121"), boundary(20)],
                          [cand("ob_0120"), cand("ob_0121")])
    assert got == {"ob_0120": (True, "persisted"), "ob_0121": (True, "shown")}
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (1, 0)


def test_cell22_zero_boundaries_ledger_without_hook_flags(tool, tmp_path):
    # Pre-#3554 hook: no in_context anywhere (None). ob_0122 in the text at s1 -> true/shown;
    # ob_0123 not in the text -> false/none. Hook fields stay None beside. 2 candidates:
    # hook_flag_present 0, hook_flag_absent 2, disagree 0 (hook null -> never counted).
    tr = [tl(1, "ob_0122 here"), tl(2, "other")]
    rc, out = run(tool, tmp_path, tr, {"prompt_surface": [lline(10, [cand("ob_0122", hook=None), cand("ob_0123", hook=None)])]})
    assert rc == 0
    cs = rows(out)[0]["candidates"]
    assert [(c["in_context_offline"], c["in_context_offline_why"], c["in_context_hook"], c["in_context_hook_why"]) for c in cs] == \
        [(True, "shown", None, None), (False, "none", None, None)]
    s = json.loads((out / "summary.json").read_text())
    assert (s["hook_flag_present"], s["hook_flag_absent"], s["disagree_with_hook"]) == (0, 2, 0)
    assert s["by_why"] == {"shown": 1, "none": 1}


def test_cell23_zero_boundaries_hook_disagrees_with_text(tool, tmp_path):
    # transcript text carries ob_0131 and ob_0133 only. Window [s1, s10).
    #   ob_0130 hook true/shown, never in text -> false/none   disagree (both non-null, differ)
    #   ob_0131 hook false/None, in text       -> true/shown   disagree
    #   ob_0132 hook None, not in text         -> false/none   no disagree (hook null)
    #   ob_0133 hook true/shown, in text       -> true/shown   agree
    # disagree 2; hook present 3 (0130, 0131, 0133), absent 1 (0132).
    tr = [tl(1, "ob_0131 and ob_0133"), tl(2, "other")]
    rc, out = run(tool, tmp_path, tr, {"prompt_surface": [lline(10, [
        cand("ob_0130", hook=True, why="shown"), cand("ob_0131", hook=False), cand("ob_0132", hook=None),
        cand("ob_0133", hook=True, why="shown")])]})
    assert rc == 0
    cs = {c["id"]: c for c in rows(out)[0]["candidates"]}
    assert (cs["ob_0130"]["in_context_offline"], cs["ob_0130"]["in_context_offline_why"],
            cs["ob_0130"]["in_context_hook"], cs["ob_0130"]["in_context_hook_why"]) == (False, "none", True, "shown")
    assert (cs["ob_0131"]["in_context_offline"], cs["ob_0131"]["in_context_offline_why"]) == (True, "shown")
    assert (cs["ob_0132"]["in_context_offline"], cs["ob_0132"]["in_context_offline_why"]) == (False, "none")
    assert (cs["ob_0133"]["in_context_offline"], cs["ob_0133"]["in_context_offline_why"]) == (True, "shown")
    s = json.loads((out / "summary.json").read_text())
    assert (s["disagree_with_hook"], s["hook_flag_present"], s["hook_flag_absent"]) == (2, 3, 1)


def test_cell24_stub_path_with_nul_byte_is_missing_not_a_crash(tool, tmp_path, capsys):
    # json escapes the NUL, so the transcript line parses and the stub path carries \x00; os.path.realpath
    # raises ValueError on it. The run must complete: stub counted missing, one stderr line, rc 0.
    rc, got, s = stub_run(tool, tmp_path, [boundary(2), tl(3, stub_text(str(tmp_path / "a\x00b.txt"))), tl(4, "ob_0140")],
                          [cand("ob_0140"), cand("ob_0141")])
    err = capsys.readouterr().err
    assert rc == 0
    assert got == {"ob_0140": (True, "shown"), "ob_0141": (False, "none")}
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (0, 1)
    assert "persisted-output" in err


DAY = 86_400_000


def test_cell25_ledger_ts_in_the_wrong_unit_warns_once_per_file_and_still_processes(tool, tmp_path, capsys):
    # Transcript spans s1..s3. prompt_surface rows: ts in SECONDS (1790849100 -> 1970 as ms: implausible),
    # a normal row at s10, ts in MICROSECONDS (T0*1000 + 10_000_000: year ~58000 as ms: implausible).
    # tool_recall: one seconds-valued row. => implausible rows 3, warnings 2 (one per FILE), all 4 rows processed.
    tr = [tl(1, "x"), boundary(2), tl(3, "y ob_0171")]

    def row(ts, route="prompt_surface"):
        r = lline(10, [cand("ob_0171")], route)
        r["ts"] = ts
        return r

    rc, out = run(tool, tmp_path, tr, {
        "prompt_surface": [row(1_790_849_100), row(at(10)), row(T0 * 1000 + 10_000_000)],
        "tool_recall": [row(1_790_849_100, "tool_recall")]})
    err = capsys.readouterr().err
    assert rc == 0
    s = json.loads((out / "summary.json").read_text())
    assert s["ledger_ts_implausible_rows"] == 3
    warn = [l for l in err.splitlines() if "implausible" in l]
    assert len(warn) == 2
    assert sum("prompt_surface.jsonl" in l for l in warn) == 1 and sum("tool_recall.jsonl" in l for l in warn) == 1
    a, b, c = rows(out)
    # seconds row: t in 1970, before the transcript start -> no boundary, empty window -> false/none.
    assert (a["boundary_index"], a["candidates"][0]["in_context_offline"], a["candidates"][0]["in_context_offline_why"]) == (None, False, "none")
    assert (b["candidates"][0]["in_context_offline"], b["candidates"][0]["in_context_offline_why"]) == (True, "shown")
    # microsecond row: t at the far future -> the whole post-B window -> shown; still processed.
    assert (c["boundary_index"], c["candidates"][0]["in_context_offline_why"]) == (0, "shown")
    assert len(rows(out, "tool_recall")) == 1


def test_cell25b_ledger_ts_plausible_band_edges_and_clean_run(tool, tmp_path, capsys):
    # Band = [min transcript ts - 1 day, max transcript ts + 1 day], inclusive. Transcript min s1, max s3.
    # at(3)+DAY plausible, at(3)+DAY+1 implausible, at(1)-DAY plausible, at(1)-DAY-1 implausible -> 2.
    tr = [tl(1, "x"), boundary(2), tl(3, "y")]

    def row(ts):
        r = lline(10, [cand("ob_0172")])
        r["ts"] = ts
        return r

    rc, out = run(tool, tmp_path, tr, {"prompt_surface": [row(at(3) + DAY), row(at(3) + DAY + 1),
                                                          row(at(1) - DAY), row(at(1) - DAY - 1)]})
    assert rc == 0
    assert json.loads((out / "summary.json").read_text())["ledger_ts_implausible_rows"] == 2
    capsys.readouterr()
    (tmp_path / "clean").mkdir()
    rc, out = run(tool, tmp_path / "clean", tr, {"prompt_surface": [lline(10, [cand("ob_0172")])]})
    assert rc == 0
    assert json.loads((out / "summary.json").read_text())["ledger_ts_implausible_rows"] == 0
    assert "implausible" not in capsys.readouterr().err


def test_cell26_z_suffix_does_not_depend_on_the_interpreters_fromisoformat(strict_z, tmp_path):
    # Python < 3.11 rejects a trailing Z. `strict_z` makes the tool's datetime reject it like 3.9/3.10 do.
    # Same session twice: every timestamp (transcript, created_at) written with Z, then with +00:00.
    # B s2, t s10. ob_0150: text only at s1 (before B), created s5 -> created. ob_0151 text at s3 -> shown.
    # ob_0152 created 2020 -> none. Both runs must give these values and the same sidecar rows.
    tool = strict_z
    tr = [tl(1, "ob_0150"), boundary(2), tl(3, "ob_0151")]
    cs = [cand("ob_0150", created_at=iso(at(5))), cand("ob_0151"), cand("ob_0152", created_at="2020-01-01T00:00:00Z")]
    want = {"ob_0150": (True, "created"), "ob_0151": (True, "shown"), "ob_0152": (False, "none")}

    def plus(o):
        return json.loads(json.dumps(o).replace('Z"', '+00:00"'))

    (tmp_path / "z").mkdir()
    (tmp_path / "p").mkdir()
    rc_z, out_z = run(tool, tmp_path / "z", tr, {"prompt_surface": [lline(10, cs)]})
    rc_p, out_p = run(tool, tmp_path / "p", plus(tr), {"prompt_surface": [plus(lline(10, cs))]})
    assert (rc_z, rc_p) == (0, 0)
    for out in (out_z, out_p):
        r = rows(out)[0]
        assert r["boundary_index"] == 0
        assert {c["id"]: (c["in_context_offline"], c["in_context_offline_why"]) for c in r["candidates"]} == want
        assert json.loads((out / "summary.json").read_text())["boundaries"] == 1
    zr, pr = rows(out_z)[0], rows(out_p)[0]
    assert zr["candidates"] == pr["candidates"]  # boundary_ts keeps the text it was written with


def test_cell27_attachment_file_line_after_the_boundary_is_shown(tool, tmp_path):
    # A re-attached file (`attachment` of type `file`) after B carries ob_0160 in its content -> shown.
    att = {"type": "file", "filename": "/x/notes.md", "content": {"type": "text", "file": {"content": "notes ob_0160"}}}
    tr = [boundary(2), tl(4, None, type_="attachment", attachment=att)]
    assert one(tool, tmp_path, tr, [cand("ob_0160")]) == {"ob_0160": (True, "shown")}


def test_cell27b_line_stamped_exactly_at_the_boundary_is_not_in_the_window(tool, tmp_path):
    # Window start is exclusive: a line at exactly B's timestamp (s2) is not in W. ob_0161 -> false/none;
    # ob_0162 in a line 1 ms later (s2.001) -> shown (control).
    tr = [boundary(2), tl(2, "same instant ob_0161"), tl(2.001, "just after ob_0162")]
    assert one(tool, tmp_path, tr, [cand("ob_0161"), cand("ob_0162")]) == {"ob_0161": (False, "none"), "ob_0162": (True, "shown")}


def test_cell27c_created_edges_are_start_inclusive_end_exclusive(tool, tmp_path):
    # B s2, t s10. created exactly B -> created; exactly t -> none. 1 ms inside each edge as control.
    tr = [boundary(2), tl(3, "filler")]
    got = one(tool, tmp_path, tr, [
        cand("ob_0163", created_at=naive(at(2))), cand("ob_0164", created_at=naive(at(10))),
        cand("ob_0165", created_at=naive(at(9.999))), cand("ob_0166", created_at=naive(at(1.999)))])
    assert got == {"ob_0163": (True, "created"), "ob_0164": (False, "none"),
                   "ob_0165": (True, "created"), "ob_0166": (False, "none")}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_cell27d_fifo_stub_target_is_refused_without_hanging(tool, tmp_path):
    # A FIFO inside the allowed root: opening it for reading blocks forever. The regular-file check must
    # refuse it BEFORE any open. Run in a subprocess with a 10 s timeout so a regression fails, not hangs.
    d = tmp_path / "tool-results"
    d.mkdir()
    fifo = d / "pipe"
    os.mkfifo(fifo)
    tr, routes, out = setup(tmp_path, [boundary(2), tl(3, stub_text(fifo)), tl(4, "ob_0167")],
                            {"prompt_surface": [lline(10, [cand("ob_0167"), cand("ob_0168")])]})
    try:
        p = subprocess.run([sys.executable, str(TOOL), str(tr), str(routes), "--out", str(out)],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail("tool hung on a FIFO stub target")
    assert p.returncode == 0, p.stderr
    s = json.loads((out / "summary.json").read_text())
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (0, 1)
    assert [(c["in_context_offline"], c["in_context_offline_why"]) for c in rows(out)[0]["candidates"]] == \
        [(True, "shown"), (False, "none")]


def test_cell28_persisted_roots_are_projects_not_all_of_dot_claude(tool, tmp_path, monkeypatch):
    # HOME -> a scratch home. A stub inside ~/.claude/projects is read; one inside ~/.claude/other is refused.
    home, work = tmp_path / "home", tmp_path / "work"
    (home / ".claude" / "projects" / "p").mkdir(parents=True)
    (home / ".claude" / "other").mkdir()
    work.mkdir()
    ok = home / ".claude" / "projects" / "p" / "a.txt"
    ok.write_text("ob_0169")
    no = home / ".claude" / "other" / "b.txt"
    no.write_text("ob_0170")
    monkeypatch.setenv("HOME", str(home))
    rc, got, s = stub_run(tool, work, [boundary(2), tl(3, stub_text(ok)), tl(4, stub_text(no))],
                          [cand("ob_0169"), cand("ob_0170")])
    assert rc == 0
    assert got == {"ob_0169": (True, "persisted"), "ob_0170": (False, "none")}
    assert (s["persisted_output_stubs_read"], s["persisted_output_stubs_missing"]) == (1, 1)
