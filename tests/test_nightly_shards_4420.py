"""Contract cells for .github/ci/nightly_shards.py (#4420).

The nightly suite runs as N sequential pytest processes. The shard tool splits
the collected test FILES into N contiguous groups balanced by test count.

Contract (stated before the tool existed):

C2a  the shards cover the collected file set exactly once: none dropped, none twice.
C2b  the output is stable for the same input.
C2c  a test file added under ``tests/`` or ``src/forum/tests/`` lands in exactly
     one shard with NO edit to the workflow (a hand-written file list in the
     YAML would drift silently).
C2d  no file spans two shards, and each shard is contiguous in the tool's order
     (a file's cells may rely on their predecessors in the same file having run
     in the same process).
C2e  the order is path order with ``tests/`` before ``src/forum/tests/``, whatever
     order pytest collected in (that order differs between pytest versions).
C2f  a failed collection fails the tool loudly; it never plans a partial suite.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CI_DIR = REPO / ".github" / "ci"
TOOL = CI_DIR / "nightly_shards.py"
WORKFLOW = REPO / ".github" / "workflows" / "nightly.yml"


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("nightly_shards_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _counts(n_tests=40, n_forum=10):
    out = [(f"tests/test_f{i:03d}.py", (i * 7) % 23 + 1) for i in range(n_tests)]
    out += [(f"src/forum/tests/test_g{i:03d}.py", (i * 5) % 17 + 1) for i in range(n_forum)]
    return out


def _flat(plan_shards):
    return [f for sh in plan_shards for f, _ in sh]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7])
def test_c2a_union_of_shards_is_the_full_file_set_exactly_once(shards, n):
    counts = _counts()
    parts = shards.split_shards(counts, n)
    assert len(parts) == n
    flat = _flat(parts)
    assert sorted(flat) == sorted(f for f, _ in counts)
    assert len(flat) == len(set(flat))


def test_c2a_more_shards_than_files_keeps_every_file_once(shards):
    counts = [("tests/test_a.py", 3), ("tests/test_b.py", 4)]
    parts = shards.split_shards(counts, 5)
    assert len(parts) == 5
    assert sorted(_flat(parts)) == ["tests/test_a.py", "tests/test_b.py"]


def test_c2b_output_is_stable_for_the_same_input(shards):
    counts = _counts()
    assert shards.split_shards(counts, 5) == shards.split_shards(list(counts), 5)
    assert shards.split_shards(counts, 5) == shards.split_shards(list(reversed(counts)), 5)


def test_c2e_tests_dir_before_forum_tests_whatever_the_input_order(shards):
    counts = [("src/forum/tests/test_z.py", 2), ("tests/test_b.py", 2), ("tests/test_a.py", 2)]
    flat = _flat(shards.split_shards(counts, 1))
    assert flat == ["tests/test_a.py", "tests/test_b.py", "src/forum/tests/test_z.py"]


def test_shards_are_balanced_by_test_count(shards):
    counts = [(f"tests/test_f{i:03d}.py", 10) for i in range(100)]
    sizes = [sum(c for _, c in sh) for sh in shards.split_shards(counts, 4)]
    assert sizes == [250, 250, 250, 250]
    counts = _counts(200, 40)
    total = sum(c for _, c in counts)
    biggest = max(c for _, c in counts)
    for n in (3, 4, 5, 6):
        sizes = [sum(c for _, c in sh) for sh in shards.split_shards(counts, n)]
        assert max(sizes) <= total / n + biggest, (n, sizes)


def test_c2d_no_file_spans_two_shards_and_each_shard_is_contiguous(shards):
    # Nodeid-level: every test of a file lands in the same shard.
    text = "\n".join(
        f"{f}::test_{k}" for f, c in _counts(30, 6) for k in range(c)
    )
    counts = shards.parse_collect(text)
    parts = shards.split_shards(counts, 5)
    where = {}
    for idx, sh in enumerate(parts):
        for f, _ in sh:
            assert f not in where, f"{f} spans shards {where[f]} and {idx}"
            where[f] = idx
    order = _flat(parts)
    assert order == shards.order_files([f for f, _ in counts])
    idxs = [where[f] for f in order]
    assert idxs == sorted(idxs), "shards must be contiguous runs of the ordered file list"


def test_parse_collect_reads_qq_counts_and_nodeid_lines_and_ignores_trailer(shards):
    qq = "tests/test_a.py: 3\nsrc/forum/tests/test_b.py: 2\n\n5/9 tests collected (4 deselected) in 1.2s\n"
    assert dict(shards.parse_collect(qq)) == {"tests/test_a.py": 3, "src/forum/tests/test_b.py": 2}
    q = "tests/test_a.py::test_x\ntests/test_a.py::T::test_y[1]\ntests/test_c.py::test_z\n\n3 tests collected\n"
    assert dict(shards.parse_collect(q)) == {"tests/test_a.py": 2, "tests/test_c.py": 1}


# --- real collection in a throwaway tree (C2c, C2f) -----------------------------------


def _mini_tree(root):
    (root / "tests").mkdir()
    (root / "src" / "forum" / "tests").mkdir(parents=True)
    for name, k in (("tests/test_a.py", 3), ("tests/test_b.py", 2), ("src/forum/tests/test_c.py", 4)):
        body = "".join(f"def test_{i}():\n    pass\n\n" for i in range(k))
        (root / name).write_text(body, encoding="utf-8")


def _run_plan(root, n, out):
    return subprocess.run(
        [sys.executable, str(TOOL), "plan", "--n", str(n), "--out", str(out)],
        cwd=str(root), capture_output=True, text=True, timeout=120,
    )


def _plan_files(plan_path):
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    return [[f for f in sh["files"]] for sh in plan["shards"]]


def test_c2c_a_new_file_lands_in_exactly_one_shard_without_a_workflow_edit(tmp_path):
    _mini_tree(tmp_path)
    r = _run_plan(tmp_path, 2, tmp_path / "plan1.json")
    assert r.returncode == 0, r.stdout + r.stderr
    before = [f for sh in _plan_files(tmp_path / "plan1.json") for f in sh]
    assert sorted(before) == ["src/forum/tests/test_c.py", "tests/test_a.py", "tests/test_b.py"]

    (tmp_path / "tests" / "test_new_file.py").write_text("def test_n():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "forum" / "tests" / "test_new_forum.py").write_text(
        "def test_m():\n    pass\n", encoding="utf-8")
    r = _run_plan(tmp_path, 2, tmp_path / "plan2.json")
    assert r.returncode == 0, r.stdout + r.stderr
    shards_after = _plan_files(tmp_path / "plan2.json")
    flat = [f for sh in shards_after for f in sh]
    for new in ("tests/test_new_file.py", "src/forum/tests/test_new_forum.py"):
        assert flat.count(new) == 1, (new, shards_after)
    assert len(flat) == len(set(flat)) == 5

    # ...and the workflow holds no hand-written test file list to edit.
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_" not in wf.replace("src/forum/tests/ ", "")
    assert "test_new_file" not in wf


def test_c2c_plan_cli_is_stable_across_runs(tmp_path):
    _mini_tree(tmp_path)
    assert _run_plan(tmp_path, 2, tmp_path / "p1.json").returncode == 0
    assert _run_plan(tmp_path, 2, tmp_path / "p2.json").returncode == 0
    assert (tmp_path / "p1.json").read_text() == (tmp_path / "p2.json").read_text()


def test_files_subcommand_prints_one_shard_one_path_per_line(tmp_path):
    _mini_tree(tmp_path)
    assert _run_plan(tmp_path, 2, tmp_path / "plan.json").returncode == 0
    planned = _plan_files(tmp_path / "plan.json")
    for k in (1, 2):
        r = subprocess.run(
            [sys.executable, str(TOOL), "files", "--plan", str(tmp_path / "plan.json"), "--shard", str(k)],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == planned[k - 1]


def test_c2f_failed_collection_fails_loudly_and_writes_no_plan(tmp_path):
    _mini_tree(tmp_path)
    ok = _run_plan(tmp_path, 2, tmp_path / "ok.json")
    assert ok.returncode == 0, ok.stdout + ok.stderr  # the tree plans fine until it is broken
    (tmp_path / "tests" / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    out = tmp_path / "plan.json"
    r = _run_plan(tmp_path, 2, out)
    assert r.returncode != 0
    assert not out.exists()


def test_n_must_be_positive(tmp_path):
    _mini_tree(tmp_path)
    assert _run_plan(tmp_path, 1, tmp_path / "one.json").returncode == 0
    r = _run_plan(tmp_path, 0, tmp_path / "plan.json")
    assert r.returncode != 0
    assert not (tmp_path / "plan.json").exists()
