"""#4468 (part of #4447, contract 2): the forum keeps its OWN copy of the
embedding pin table, because it is deployed from src/forum/ alone and does not
import engram_core. This file is the one binding between the two copies.

Cells:
  (a) drift: the forum table equals the engram_core table, field by field.
  (b) safetensors only: neither table pins a pickle-based weights file, so a
      refresh cannot point at weights whose constructor unpickles before any
      hash is compared (Zephyr's #4467 note 1).
  (c) shape: 40-hex revision, exactly ten files, each hash 64 lowercase hex.

The forum module is loaded by file path (it is not on the root suite's path).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import engram_core

FORUM_EMBEDDINGS = (
    Path(__file__).resolve().parent.parent / "src" / "forum" / "embeddings.py"
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PICKLE_SUFFIXES = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle")


@pytest.fixture(scope="module")
def forum_pin():
    spec = importlib.util.spec_from_file_location(
        "forum_embeddings_pin_4468", str(FORUM_EMBEDDINGS)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._MINILM_PIN


@pytest.fixture(params=["core", "forum"])
def table(request, forum_pin):
    return engram_core._MINILM_PIN if request.param == "core" else forum_pin


def test_a_forum_pin_equals_engram_core_pin(forum_pin):
    core = engram_core._MINILM_PIN
    assert forum_pin["repo_id"] == core["repo_id"]
    assert forum_pin["revision"] == core["revision"]
    assert forum_pin["sha256"] == core["sha256"]
    assert forum_pin == core


def test_b_only_safetensors_weights_are_pinned(table):
    names = table["sha256"]
    assert "model.safetensors" in names
    pickled = [n for n in names if n.lower().endswith(PICKLE_SUFFIXES)]
    assert pickled == [], f"pickle-based weights pinned: {pickled}"


def test_c_pin_shape(table):
    assert HEX40.match(table["revision"])
    assert len(table["sha256"]) == 10
    for name, digest in table["sha256"].items():
        assert HEX64.match(digest), f"{name}: {digest!r}"
