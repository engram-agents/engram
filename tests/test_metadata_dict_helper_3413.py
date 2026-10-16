"""Contract for engram_core._metadata_dict (#3413) -- written before the helper.

Contract:
  M1 None, "" and any other falsy raw value -> {}.
  M2 a str holding a JSON *object* -> that object, parsed.
  M3 a str that is not valid JSON -> {} (no raise).
  M4 a str holding valid JSON that is NOT an object ([1,2], "str", 5, null,
     true, 1.5, "", []) -> {} (no raise).
  M5 an already-parsed dict is returned as-is (same object).
  M6 an already-parsed non-dict (list, int, ...) -> {}.
  M7 str only: bytes / bytearray are NOT decoded -> {} (nodes.metadata is TEXT;
     decoding bytes would widen the contract for no caller).
  M8 the result is always a dict, so `.get`, `[k] = v` and `k in` are safe.
  M9 pure: no logging on the non-object / invalid path.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from engram_core import _metadata_dict  # noqa: E402


def test_M1_falsy_raw_values():
    assert _metadata_dict(None) == {}
    assert _metadata_dict("") == {}
    assert _metadata_dict(0) == {}
    assert _metadata_dict(False) == {}


@pytest.mark.parametrize("raw,expected", [
    ("{}", {}),
    ('{"a":1}', {"a": 1}),
    ('{"a":{"b":[1,2]},"c":null}', {"a": {"b": [1, 2]}, "c": None}),
])
def test_M2_object_blobs_parse(raw, expected):
    assert _metadata_dict(raw) == expected


@pytest.mark.parametrize("raw", ["{", "not json", "{'a':1}", "[1,", "\x00"])
def test_M3_invalid_json_is_empty(raw):
    assert _metadata_dict(raw) == {}


@pytest.mark.parametrize("raw", [
    "[1,2]", '"str"', "5", "null", "true", "false", "1.5", '""', "[]", "-0",
])
def test_M4_valid_json_non_object_is_empty(raw):
    out = _metadata_dict(raw)
    assert out == {} and isinstance(out, dict)


def test_M5_parsed_dict_returned_as_is():
    d = {"a": 1}
    assert _metadata_dict(d) is d


@pytest.mark.parametrize("val", [[1, 2], [], 5, 1.5, True, ("a",), {1, 2}])
def test_M6_parsed_non_dict_is_empty(val):
    assert _metadata_dict(val) == {}


@pytest.mark.parametrize("val", [b'{"a":1}', bytearray(b'{"a":1}')])
def test_M7_bytes_not_decoded(val):
    assert _metadata_dict(val) == {}


@pytest.mark.parametrize("raw", ["[1,2]", '"str"', "5", "null", None, "", "{"])
def test_M8_result_is_safe_to_read_and_write(raw):
    m = _metadata_dict(raw)
    assert m.get("x") is None
    assert "field_traces" not in m
    m["field_traces"] = {}
    assert m == {"field_traces": {}}


def test_M9_no_logging(caplog):
    with caplog.at_level(logging.DEBUG):
        for raw in ("[1,2]", "{", None, "5"):
            _metadata_dict(raw)
    assert caplog.records == []


def test_M10_two_calls_return_independent_dicts():
    # An empty result must not be a shared mutable singleton.
    a, b = _metadata_dict(None), _metadata_dict("[1]")
    a["k"] = 1
    assert b == {} and _metadata_dict(None) == {}
