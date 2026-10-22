"""Tests for tools/check_no_cuda_wheels.py (#4004): the CPU-torch guard."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_no_cuda_wheels.py"


def _run(tmp_path: Path, pip_body: str):
    fake = tmp_path / "pip"
    fake.write_text("#!/bin/sh\n" + pip_body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env
    )


def test_clean_env_exits_zero(tmp_path):
    r = _run(tmp_path, "echo torch==2.14.1+cpu\necho numpy==2.0.0\n")
    assert r.returncode == 0, r.stdout + r.stderr


def test_nvidia_wheel_exits_one_with_error_line(tmp_path):
    r = _run(tmp_path, "echo torch==2.14.1\necho nvidia-cublas-13.1.1.3\n")
    assert r.returncode == 1
    assert "nvidia-cublas" in r.stdout
    assert "::error::CUDA wheels installed; CPU torch expected" in r.stdout
    assert "#4004" in r.stdout


def test_triton_exits_one(tmp_path):
    assert _run(tmp_path, "echo Triton==3.8.0\n").returncode == 1


def test_lookalike_names_do_not_trip(tmp_path):
    # 'triton-client' is not 'triton==' ; 'pynvidia-x' does not start with nvidia-
    r = _run(tmp_path, "echo tritonclient==2.0\necho pynvidia-x==1\n")
    assert r.returncode == 0


def test_pip_failure_fails_closed(tmp_path):
    assert _run(tmp_path, "echo boom >&2\nexit 3\n").returncode == 2


def test_underscore_and_dot_spellings_exit_one(tmp_path):
    # pip freeze may print the installed (un-normalized) name.
    assert _run(tmp_path, "echo nvidia_cublas_cu12==12.1.3.1\n").returncode == 1
    assert _run(tmp_path, "echo Nvidia.Nccl.Cu12==2.1\n").returncode == 1
