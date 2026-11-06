"""Tests for tools/ws4/merge_spans.py (engram-alpha#2753, design owner's
condition 3: spans reach the adjudicators as ONE list in fire-id order, the
strata not grouped). Synthetic fixtures in tmp_path only."""
import contextlib
import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.ws4 import merge_spans  # noqa: E402


def _span(directory, fire_id, body=None):
    directory.mkdir(parents=True, exist_ok=True)
    data = body if body is not None else f"# Fire {fire_id}\n\nfixture body for {fire_id}\n".encode("utf-8")
    (directory / f"{fire_id}.md").write_bytes(data)
    return data


def _merge(out_dirs, dest):
    argv = []
    for o in out_dirs:
        argv += ["--out", str(o)]
    argv += ["--dest", str(dest)]
    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        rc = merge_spans.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _two_outs(tmp_path):
    """Primary fires b, d, f; sub-agent fires a, c, e -- interleaved on purpose."""
    out1, out2 = tmp_path / "out1", tmp_path / "out2"
    for fid in ("fire-b", "fire-f"):
        _span(out1 / "spans", fid)
    _span(out1 / "subagent" / "spans", "fire-a")
    _span(out1 / "subagent" / "spans", "fire-e")
    _span(out2 / "spans", "fire-d")
    _span(out2 / "subagent" / "spans", "fire-c")
    return [out1, out2]


def test_a_merged_dir_mixes_the_strata_in_pure_fire_id_order_and_names_no_stratum(tmp_path):
    outs = _two_outs(tmp_path)
    dest = tmp_path / "dest"
    rc, _, err = _merge(outs, dest)
    assert rc == 0, err
    expected = [f"fire-{c}.md" for c in "abcdef"]
    assert (dest / "order.txt").read_text(encoding="utf-8").splitlines() == expected
    assert sorted(p.name for p in (dest / "spans").iterdir()) == expected
    # nothing else is written
    assert sorted(p.name for p in dest.iterdir()) == ["order.txt", "spans"]
    # nothing in dest names a stratum, in a path or in any content
    for p in dest.rglob("*"):
        text = str(p.relative_to(dest))
        if p.is_file():
            text += p.read_text(encoding="utf-8")
        for word in ("subagent", "stratum", "agent_type"):
            assert word not in text, (word, p)


def test_b_duplicate_fire_id_is_refused_and_nothing_is_written(tmp_path):
    out1, out2 = tmp_path / "out1", tmp_path / "out2"
    _span(out1 / "spans", "fire-a")
    _span(out2 / "subagent" / "spans", "fire-a")
    dest = tmp_path / "dest"
    rc, _, err = _merge([out1, out2], dest)
    assert rc == 2
    assert "fire-a" in err and "twice" in err
    assert not dest.exists()


def test_c_non_empty_dest_is_refused_and_left_untouched(tmp_path):
    outs = _two_outs(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine", encoding="utf-8")
    rc, _, err = _merge(outs, dest)
    assert rc == 2 and str(dest) in err
    assert [p.name for p in dest.iterdir()] == ["keep.txt"]
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "mine"
    # an existing but EMPTY dest is fine
    empty = tmp_path / "empty"
    empty.mkdir()
    rc2, _, err2 = _merge(outs, empty)
    assert rc2 == 0, err2


def test_d_copies_are_byte_identical_to_the_sources(tmp_path):
    out1 = tmp_path / "out1"
    # bytes a text round-trip would change: CRLF, a lone CR, non-ASCII, no trailing newline
    a = _span(out1 / "spans", "fire-a", "line1\r\nline2\rcafé → end".encode("utf-8"))
    b = _span(out1 / "subagent" / "spans", "fire-b", b"\x00\xff raw bytes\r\n")
    dest = tmp_path / "dest"
    rc, _, err = _merge([out1], dest)
    assert rc == 0, err
    assert (dest / "spans" / "fire-a.md").read_bytes() == a
    assert (dest / "spans" / "fire-b.md").read_bytes() == b


def test_e_out_holding_only_per_day_subdirs_is_refused_and_nothing_is_written(tmp_path):
    # the parent of per-day directories passed as --out: neither spans/ nor subagent/spans/
    run = tmp_path / "run"
    _span(run / "2026-10-01" / "spans", "fire-a")
    _span(run / "2026-10-02" / "spans", "fire-b")
    dest = tmp_path / "dest"
    rc, out, err = _merge([run], dest)
    assert rc == 2
    assert str(run) in err
    assert not dest.exists()
    assert out == ""
    # a pre-created empty dest stays empty
    empty = tmp_path / "empty"
    empty.mkdir()
    rc2, _, err2 = _merge([run], empty)
    assert rc2 == 2 and str(run) in err2
    assert list(empty.iterdir()) == []


def test_f_one_good_out_plus_one_without_spans_dirs_writes_nothing(tmp_path):
    good = tmp_path / "good"
    _span(good / "spans", "fire-a")
    bad = tmp_path / "bad"
    bad.mkdir()
    dest = tmp_path / "dest"
    for order in ([good, bad], [bad, good]):
        rc, _, err = _merge(order, dest)
        assert rc == 2
        assert str(bad) in err
        assert not dest.exists()


def test_g_out_with_an_empty_spans_dir_is_accepted_and_reported_as_zero(tmp_path):
    good = tmp_path / "good"
    _span(good / "spans", "fire-a")
    _span(good / "subagent" / "spans", "fire-b")
    quiet = tmp_path / "quiet"
    (quiet / "spans").mkdir(parents=True)
    dest = tmp_path / "dest"
    rc, out, err = _merge([good, quiet], dest)
    assert rc == 0, err
    lines = out.splitlines()
    assert len(lines) == 3, lines
    assert str(good) in lines[0] and "2 spans" in lines[0]
    assert str(quiet) in lines[1] and "0 spans" in lines[1]
    assert lines[2] == f"merged 2 spans into {dest / 'spans'}"
    assert (dest / "order.txt").read_text(encoding="utf-8").splitlines() == ["fire-a.md", "fire-b.md"]


def test_h_only_subagent_spans_dir_present_is_accepted(tmp_path):
    out1 = tmp_path / "out1"
    (out1 / "subagent" / "spans").mkdir(parents=True)
    dest = tmp_path / "dest"
    rc, out, err = _merge([out1], dest)
    assert rc == 0, err
    assert out.splitlines()[-1] == f"merged 0 spans into {dest / 'spans'}"
