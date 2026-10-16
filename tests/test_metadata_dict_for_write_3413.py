"""Contract for engram_core._metadata_dict_for_write (#3413, write half) --
written before the helper.

A write site parses `nodes.metadata`, mutates it, re-dumps it. For a blob that
is not a JSON object the site treats it as {} and writes back a DICT. Replacing
on-disk bytes silently is a suppression, so the write-side variant says so.

Contract:
  W1 raw is a dict, or a str holding a JSON object -> (that dict, None), and
     nothing is logged.
  W2 raw is None / "" / any other falsy value -> ({}, None), nothing logged:
     there was no blob, so nothing carrying information is replaced.
  W3 raw is a str holding valid JSON that is not an object (`[1,2]`, `"str"`,
     `5`, `null`, `true`, `1.5`, `""`, `[]`) -> ({}, warning), and EXACTLY ONE
     line goes to stderr naming the node id and the blob's type.
  W4 raw is a str that is not valid JSON -> ({}, warning) and one stderr line:
     an invalid blob IS a replaced non-object (decision stated in the PR).
  W5 any other truthy non-str non-dict value (a parsed list, bytes) -> ({},
     warning), one stderr line.
  W6 the stderr line also names the calling tool; `warning` text names the
     node id and the tool.
  W7 the log is printed to sys.stderr, never to stdout (stdout is the MCP
     channel), once per call.
  W8 the returned dict is a fresh mutable dict, so `.get`, `[k] = v`, `k in`
     are safe and nothing is shared between calls.
  W9 the read helper `_metadata_dict` stays silent on the same inputs.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from engram_core import _metadata_dict, _metadata_dict_for_write  # noqa: E402

NON_OBJECT = [
    ("[1,2]", "list"), ('"str"', "str"), ("5", "int"), ("null", "null"),
    ("true", "bool"), ("1.5", "float"), ('""', "str"), ("[]", "list"),
]


def test_W1_object_blobs_pass_through_silently(capsys):
    d = {"a": 1}
    out, warn = _metadata_dict_for_write(d, "ob_0001", "t")
    assert out is d and warn is None
    out, warn = _metadata_dict_for_write('{"a":{"b":[1]}}', "ob_0001", "t")
    assert out == {"a": {"b": [1]}} and warn is None
    out, warn = _metadata_dict_for_write("{}", "ob_0001", "t")
    assert out == {} and warn is None
    cap = capsys.readouterr()
    assert cap.err == "" and cap.out == ""


@pytest.mark.parametrize("raw", [None, "", 0, False, b"", []])
def test_W2_absent_or_falsy_blob_is_not_a_replacement(raw, capsys):
    out, warn = _metadata_dict_for_write(raw, "ob_0001", "t")
    assert out == {} and warn is None
    cap = capsys.readouterr()
    assert cap.err == "" and cap.out == ""


@pytest.mark.parametrize("raw,tname", NON_OBJECT)
def test_W3_non_object_blob_replaced_warns_and_logs_once(raw, tname, capsys):
    out, warn = _metadata_dict_for_write(raw, "ob_0123", "engram_update_person")
    assert out == {} and isinstance(out, dict)
    assert warn and "ob_0123" in warn and "engram_update_person" in warn
    cap = capsys.readouterr()
    lines = [x for x in cap.err.splitlines() if x.strip()]
    assert len(lines) == 1, cap.err
    assert "ob_0123" in lines[0] and tname in lines[0]
    assert "engram_update_person" in lines[0]
    assert cap.out == ""


def test_W4_invalid_json_is_a_replaced_non_object(capsys):
    out, warn = _metadata_dict_for_write("{not json", "ob_0007", "t")
    assert out == {} and warn and "ob_0007" in warn
    lines = [x for x in capsys.readouterr().err.splitlines() if x.strip()]
    assert len(lines) == 1 and "ob_0007" in lines[0] and "invalid JSON" in lines[0]


@pytest.mark.parametrize("raw,tname", [([1, 2], "list"), (b"{}", "bytes"), (5, "int")])
def test_W5_other_truthy_non_str_non_dict(raw, tname, capsys):
    out, warn = _metadata_dict_for_write(raw, "ob_0009", "t")
    assert out == {} and warn
    lines = [x for x in capsys.readouterr().err.splitlines() if x.strip()]
    assert len(lines) == 1 and tname in lines[0] and "ob_0009" in lines[0]


def test_W8_result_is_mutable_dict_and_not_shared():
    a, _ = _metadata_dict_for_write("[1]", "ob_0001", "t")
    a["k"] = 1
    b, _ = _metadata_dict_for_write("[1]", "ob_0001", "t")
    assert b == {} and "k" not in b


@pytest.mark.parametrize("raw,_t", NON_OBJECT)
def test_W9_read_helper_stays_silent(raw, _t, capsys):
    assert _metadata_dict(raw) == {}
    cap = capsys.readouterr()
    assert cap.err == "" and cap.out == ""
