"""Contract tests: DeepSeek ids in the exact-id model table (part of #3976).

Written BEFORE the implementation (contract-first).  D1-D4 are in the dispatch
spec.  The lineage mapping is an explicit EXACT-id table; no prefix or regex
rule exists, so every unknown DeepSeek-looking id must refuse.

FIXTURE PROVENANCE.  The model attachment row is the seat's verbatim transcript
line (DeepSeek through Claude Code; attachment source, the signer's primary
reader).  For the assistant-row fallback the seat supplied ONLY the substring
``"message":{"id":"8ae5bb67-5280-41ba-afd2-aa89a72d0941","type":"message",
"role":"assistant","model":"deepseek-flash","content":[...]}``; every other key
of those rows (the outer ``type``, the empty ``content``) is a FIXTURE GUESS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engram_core  # noqa: E402
import engram_trust  # noqa: E402
from test_engram_signature import (  # noqa: E402,F401
    _build, _clean_env, _refused, _set_config, _write_jsonl, env)

# Verbatim from the seat.
SEAT_ATTACHMENT_ROW = json.loads(
    '{"type":"model","identity":{"modelId":"deepseek-flash[1m]",'
    '"marketingName":null,"knowledgeCutoff":null},'
    '"text":"You are powered by the model deepseek-flash[1m]."}')


def _att(model_id="deepseek-flash[1m]"):
    # The seat's row, wrapped as the attachment the reader looks for.
    row = json.loads(json.dumps(SEAT_ATTACHMENT_ROW))
    row["identity"]["modelId"] = model_id
    return {"attachment": row}


def _asst(model_id="deepseek-flash"):
    # Only the "message" substring is the seat's; the outer type and the empty
    # content list are fixture guesses.
    return {"type": "assistant", "message": {
        "id": "8ae5bb67-5280-41ba-afd2-aa89a72d0941", "type": "message",
        "role": "assistant", "model": model_id, "content": []}}


# ------------------------------------------------------------------ D1

@pytest.mark.parametrize("mid", [
    "deepseek-flash", "deepseek-flash[1m]", "deepseek-v4.1-flash",
    "deepseek-v4.1-flash:cloud", "deepseek-v4.1-flash:cloud[1m]",
    "deepseek-flash "])
def test_d1_known_ids_map_to_deepseek_flash(mid):
    assert engram_core.model_id_to_lineage(mid) == "deepseek:flash"


@pytest.mark.parametrize("mid", [
    "deepseek-flash2", "deepseek-flash-pro", "deepseek-v4.2-flash",
    "deepseek-pro", "deepseek-", "DeepSeek-Flash", "deepseek",
    "glm-5.3:cloud[1m]", "<synthetic>"])
def test_d1_unknown_ids_refuse(mid):
    assert engram_core.model_id_to_lineage(mid) == ""


# ------------------------------------------------------------------ D2

def test_d2_bare_flash_version_is_empty_never_guessed():
    assert engram_trust._parse_model_id("deepseek-flash") == {
        "provider": "deepseek", "family": "flash", "version": ""}


@pytest.mark.parametrize("mid", ["deepseek-v4.1-flash",
                                 "deepseek-v4.1-flash:cloud"])
def test_d2_versioned_ids_read_version_from_the_id(mid):
    assert engram_trust._parse_model_id(mid) == {
        "provider": "deepseek", "family": "flash", "version": "4.1"}


# ------------------------------------------------------------------ D3

def test_d3_seat_row_signs_with_deepseek_lineage(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_att()])
    r = _build(env)
    assert r.ok is True and "deepseek-flash[1m]" in r.line


def test_d3_seat_row_with_foreign_self_lineage_refuses(env):
    _set_config(env, self_lineage="anthropic:opus")
    _write_jsonl(env.transcript, [_att()])
    r = _refused(env, "model_family")
    assert "deepseek:flash" in r.reason and "anthropic:opus" in r.reason


def test_d3_deepseek_pro_attachment_refuses(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_att("deepseek-pro[1m]")])
    r = _refused(env, "model_family")
    assert "deepseek-pro" in r.reason


# ------------------------------------------------------------------ D4

def test_d4_assistant_rows_only_sign(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst(), _asst()])
    r = _build(env)
    assert r.ok is True and "deepseek-flash" in r.line


def test_d4_synthetic_newest_uses_older_real_row(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst("<synthetic>")])
    r = _build(env)
    assert r.ok is True and "deepseek-flash" in r.line
    assert "<synthetic>" not in r.line


def test_d4_only_synthetic_rows_refuse_no_model_source(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("<synthetic>"), _asst("<synthetic>")])
    r = _refused(env, "model")
    assert "every assistant row in" in r.reason
    assert "no usable model id" in r.reason


def test_d4_only_deepseek_pro_assistant_rows_refuse_as_unmapped_id(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-pro"), _asst("deepseek-pro")])
    # the newest real id is unmapped: it decides, and the refusal names it
    r = _refused(env, "model")
    assert "deepseek-pro" in r.reason and "does not map to a lineage" in r.reason


def test_d4_newest_unmapped_id_refuses_never_falls_back_to_older_mapped(env):
    # round 2 (inverts the round-1 cell that codified the bug): the newest real
    # row is the running model, so an unmapped id refuses instead of letting an
    # older mapped row sign
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst("deepseek-pro")])
    r = _refused(env, "model")
    assert "deepseek-pro" in r.reason and "does not map to a lineage" in r.reason


# ------------------------------------------------------------ round 2 (G2)

def test_g2a_synthetic_newest_resolves_older_deepseek_flash(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst("<synthetic>")])
    r = _build(env)
    assert r.ok is True and "deepseek-flash" in r.line
    assert "<synthetic>" not in r.line


def test_g2g_only_synthetic_rows_say_every_assistant_row_was_skipped(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("<synthetic>"), _asst(" <synthetic> ")])
    r = _refused(env, "model")
    assert "every assistant row in" in r.reason
    assert "placeholder, blank or non-string model (no usable model id)" in r.reason
    assert "cannot determine the running model" in r.reason


def test_g2b_newest_glm_refuses_naming_it(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst("glm-5.2")])
    r = _refused(env, "model")
    assert "glm-5.2" in r.reason and "does not map to a lineage" in r.reason


def test_g2c_newest_same_model_resolves(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst("deepseek-flash")])
    r = _build(env)
    assert r.ok is True and "deepseek-flash" in r.line


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_g2ef_blank_or_nonstring_newest_skipped(env, bad):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst(bad)])
    r = _build(env)
    assert r.ok is True and "deepseek-flash" in r.line


@pytest.mark.parametrize("mid", ["deepseek-flash\n", "deepseek-flash\t"])
def test_g2h_trailing_whitespace_spellings_map(mid):
    assert engram_core.model_id_to_lineage(mid) == "deepseek:flash"


# ------------------------------------------------------------ round 3 (H)

@pytest.mark.parametrize("newest", [
    "<synthetic2>", "synthetic", "xsynthetic", "<SYNTHETIC>"])
def test_h1_placeholder_is_matched_exactly_near_misses_refuse(env, newest):
    # only the exact "<synthetic>" (after strip) is skipped; a near miss is a
    # real, unmapped id: it decides and the refusal names it
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst(newest)])
    r = _refused(env, "model")
    assert repr(newest) in r.reason and "does not map to a lineage" in r.reason


def test_h2_unmapped_refusal_says_usable_model_id(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst("deepseek-flash"), _asst("glm-5.2"),
                                  _asst("<synthetic>")])
    r = _refused(env, "model")
    assert "the newest assistant row with a usable model id" in r.reason
    assert "glm-5.2" in r.reason


def test_h3_no_assistant_rows_at_all_keeps_the_old_text(env):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [{"type": "user", "message": {"content": "hi"}}])
    r = _refused(env, "model")
    assert "neither a model attachment row nor an assistant row" in r.reason
    assert "every assistant row" not in r.reason


@pytest.mark.parametrize("bad", ["", None, 5])
def test_h3_blank_or_nonstring_only_rows_use_the_new_text(env, bad):
    _set_config(env, self_lineage="deepseek:flash")
    _write_jsonl(env.transcript, [_asst(bad)])
    r = _refused(env, "model")
    assert "every assistant row in" in r.reason
    assert "neither a model attachment row" not in r.reason
