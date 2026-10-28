"""#4221: the random sk-ant-usr case in test_backup_remote_repair_4205 needs a fixed id.

Under pytest 8.x a value built from os.urandom becomes the test id, so two
collections disagree and every pytest-xdist run aborts at collection. Under
pytest 9.x the same case is id'd ``secret0`` and the two collections agree.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "tests/test_backup_remote_repair_4205.py"
FUNC = "test_repair_refuses_on_each_secret_form"
EXPECTED = f"{TARGET}::{FUNC}[sk-ant-usr-random]"


def _collect():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", TARGET],
        cwd=REPO, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"collection exited {r.returncode}: {r.stderr}\n{r.stdout}"
    return [ln for ln in r.stdout.splitlines() if "::" in ln]


def _secret_ids(ids):
    return [i for i in ids if f"::{FUNC}[" in i]


def test_c1_random_case_has_fixed_id():
    """C1: fails on the unfixed file under pytest 8 (value id) and 9 (``secret0``)."""
    assert EXPECTED in _collect()


def test_c2_two_collections_identical():
    """C2: two independent collections agree and are non-empty.

    Bound: on the unfixed file this fails under pytest 8 only; under pytest 9
    it passes on unfixed code.
    """
    a, b = _collect(), _collect()
    assert len(_secret_ids(a)) >= 5
    assert a == b


def test_c3_random_value_kept():
    """C3: the random case is a ParameterSet with id sk-ant-usr-random and a 101-char sk-ant-usr- value."""
    spec = importlib.util.spec_from_file_location("_t4205_probe", REPO / TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mark = next(m for m in mod.test_repair_refuses_on_each_secret_form.pytestmark
                if m.name == "parametrize")
    first = mark.args[1][0]
    assert hasattr(first, "values"), "random case is a bare value, not a ParameterSet"
    assert first.id == "sk-ant-usr-random"
    value = first.values[0]
    assert value.startswith("sk-ant-usr-")
    assert len(value) == 101
