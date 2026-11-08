"""Contract cells for the memrun advisory PreToolUse hook (engram-alpha#4435 item 2).

Each cell drives the hook as a subprocess with JSON on stdin and a controlled
env. The real /home/agents-shared/bin/memrun and the real /proc/meminfo are
never consulted: the ENGRAM_MEMRUN_PATH and ENGRAM_MEMINFO_PATH knobs point at
fixtures.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_SRC = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK = HOOKS_SRC / "engram-bash-memrun-advisory.py"

MEMINFO = "MemTotal:       48000000 kB\nMemFree:         1000000 kB\nMemAvailable:   12000000 kB\n"


def run_hook(command, *, env_extra=None, stdin=None, hook=HOOK):
    # Minimal env: PATH has no /home/agents-shared/bin, CLAUDE_PLUGIN_ROOT is unset.
    env = {"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()}
    env.update(env_extra or {})
    if stdin is None:
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run([sys.executable, str(hook)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=30)


def ctx(p):
    return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]


class TestCells(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.meminfo = cls.tmp / "meminfo"
        cls.meminfo.write_text(MEMINFO)
        cls.memrun = cls.tmp / "memrun"
        cls.memrun.write_text("#!/bin/sh\n")
        cls.memrun.chmod(0o755)
        cls.env = {"ENGRAM_MEMRUN_PATH": str(cls.memrun), "ENGRAM_MEMINFO_PATH": str(cls.meminfo)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fires(self, cmd, env=None):
        p = run_hook(cmd, env_extra=env or self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip(), f"expected advisory for {cmd!r}")
        self.assertIn("[memrun-advisory]", ctx(p))
        return p

    def silent(self, cmd, env=None):
        p = run_hook(cmd, env_extra=env or self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "", f"expected silence for {cmd!r}")

    def test_c1_fires_on_heavy_shapes(self):
        for cmd in [
            "pytest -n auto tests/",
            "python3 -m pytest -n 4 x",
            "ENGRAM_NO_EMBEDDINGS=1 pytest -n=8",
            "pytest --numprocesses 4",
            "python -m ltx_pipelines.render a",
            "rembg i a.png b.png",
            "ls *.png | xargs -P4 -n1 rembg i",
            "python -m rembg i a b &",
        ]:
            with self.subTest(cmd=cmd):
                self.fires(cmd)

    def test_c2_silent_when_memrun_prefixed(self):
        for cmd in [
            "/home/agents-shared/bin/memrun --need 13G -- pytest -n 4",
            "memrun --need 2G -- rembg i a b",
        ]:
            with self.subTest(cmd=cmd):
                self.silent(cmd)

    def test_c3_fires_when_one_of_two_uncovered(self):
        self.fires("memrun --need 2G -- rembg i a b; pytest -n 4")

    def test_c4_silent_on_unrelated_and_near_miss(self):
        for cmd in ["pytest tests/x.py", "git log --grep rembg", 'echo "pytest -n 4"',
                    "grep -n pytest notes.md", "ls", "cat ltx_pipelines.md"]:
            with self.subTest(cmd=cmd):
                self.silent(cmd)

    def test_c5_memrun_file_probed_not_path(self):
        # PATH is /usr/bin:/bin only (run_hook), the file exists: the full path must print.
        c = ctx(self.fires("pytest -n 4"))
        self.assertIn(f"{self.memrun} --need", c)
        self.assertIn("/usr/bin/time -v", c)

    def test_c6_memrun_absent_or_not_executable(self):
        absent = dict(self.env, ENGRAM_MEMRUN_PATH=str(self.tmp / "nope"))
        c = ctx(self.fires("pytest -n 4", absent))
        self.assertNotIn("--need", c)
        self.assertIn("no host gate installed here", c)
        noexec = self.tmp / "memrun_noexec"
        noexec.write_text("x")
        noexec.chmod(0o644)
        c = ctx(self.fires("pytest -n 4", dict(self.env, ENGRAM_MEMRUN_PATH=str(noexec))))
        self.assertNotIn("--need", c)
        self.assertIn("no host gate installed here", c)

    def test_c7_meminfo(self):
        self.assertIn("MemAvailable: 11.4 GiB of 45.8 GiB", ctx(self.fires("pytest -n 4")))
        gone = dict(self.env, ENGRAM_MEMINFO_PATH=str(self.tmp / "nope"))
        self.assertIn("MemAvailable: unknown on this platform", ctx(self.fires("pytest -n 4", gone)))

    def test_c8_never_blocks(self):
        out = json.loads(self.fires("pytest -n 4").stdout)
        self.assertNotIn("decision", out)
        self.assertNotIn("decision", out["hookSpecificOutput"])
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        for stdin in ["garbage{", json.dumps({"tool_input": "x"}),
                      json.dumps({"tool_input": {"command": ""}}),
                      json.dumps({"tool_input": {"command": 5}}),
                      json.dumps({"tool_input": {"command": 'pytest -n 4 "unbalanced'}})]:
            with self.subTest(stdin=stdin):
                q = run_hook("", env_extra=self.env, stdin=stdin)
                self.assertEqual(q.returncode, 0)
                self.assertEqual(q.stdout.strip(), "")

    def test_c9_bounded(self):
        self.assertLess(len(ctx(self.fires("pytest -n 4"))), 1200)
        absent = dict(self.env, ENGRAM_MEMRUN_PATH=str(self.tmp / "nope"))
        self.assertLess(len(ctx(self.fires("pytest -n 4", absent))), 1200)

    def test_c10_deployed_flat_layout(self):
        # Pattern: tests/test_hook_emitter_path_resolution.py copies hooks flat into a temp hooks/ dir.
        hooks = self.tmp / "plugin" / "hooks"
        hooks.mkdir(parents=True)
        for name in ("engram-bash-memrun-advisory.py", "_hooklib.py", "_harness.py"):
            shutil.copy(HOOKS_SRC / name, hooks / name)
        p = run_hook("pytest -n auto tests/", env_extra=self.env,
                     hook=hooks / "engram-bash-memrun-advisory.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("[memrun-advisory]", ctx(p))

    # ---- Round 2 (W1-W5) ----

    def test_w1_wrappers_fire(self):
        for cmd in [
            "env X=1 pytest -n 4",
            "env ENGRAM_NO_EMBEDDINGS=1 pytest -n auto",
            "env -i -u FOO X=1 pytest -n 4",
            "uv run pytest -n 4",
            "poetry run pytest -n 4",
            "pipenv run pytest -n 4",
            "timeout 600 pytest -n 4",
            "nice -n 10 pytest -n 4",
            "nice -10 pytest -n 4",
            "time pytest -n 4",
            "command pytest -n 4",
            "exec pytest -n 4",
            "sudo pytest -n 4",
            "sudo -u bob pytest -n 4",
            "bash -c 'pytest -n 4'",
            'sh -c "python -m pytest -n 4"',
            "bash -lc 'cd x && pytest -n 4'",
        ]:
            with self.subTest(cmd=cmd):
                self.fires(cmd)

    def test_w1_wrapped_memrun_and_known_silent(self):
        for cmd in [
            "timeout 600 memrun --need 4G -- pytest -n 4",
            "bash -c 'memrun --need 4G -- rembg i a b'",
            "tox -- -n 4",
            "make test-parallel",
            "echo `pytest -n 4`",
        ]:
            with self.subTest(cmd=cmd):
                self.silent(cmd)

    def test_w1_shell_c_depth_bounded(self):
        # Built with shlex.join so quoting is exact: depth <= 3 fires, a 4th level is silent.
        import shlex
        inner = "pytest -n 4"
        for _ in range(3):
            inner = shlex.join(["bash", "-c", inner])
        self.fires(inner)
        self.silent(shlex.join(["bash", "-c", inner]))

    def test_w2_heredoc(self):
        self.silent("cat > f.sh <<EOT\npytest -n 4\nEOT")
        self.silent("cat > f.sh <<-EOT\npytest -n 4\nEOT")
        self.fires("pytest -n 4 <<EOT\nx\nEOT")

    def test_w3_shows_only_first_uncovered_segment(self):
        c = ctx(self.fires("cd x && pytest -n 4"))
        self.assertIn("-- pytest -n 4", c)
        self.assertNotIn("cd x", c)
        self.assertIn("prefix the heavy part:", c)

    def test_w4_why_sentence_hedged(self):
        c = ctx(self.fires("pytest -n 4"))
        self.assertIn("can stop", c)

    def test_w5_n_zero_one_disable_xdist(self):
        for cmd in ["pytest -n 0", "pytest -n 1", "pytest -n0", "pytest -n=1",
                    "pytest --numprocesses=0", "pytest --numprocesses 1",
                    "python -m pytest -n 1 x"]:
            with self.subTest(cmd=cmd):
                self.silent(cmd)
        for cmd in ["pytest -n 2", "pytest -n auto", "pytest -n logical",
                    "pytest --numprocesses=2", "pytest -n 0 -n 4"]:
            with self.subTest(cmd=cmd):
                self.fires(cmd)


    def test_d1_command_substitution_pinned(self):
        for cmd in ["echo $(pytest -n 4)", "x=$(pytest -n 4)"]:
            with self.subTest(cmd=cmd):
                self.fires(cmd)
        for cmd in ["echo `pytest -n 4`", 'echo "$(pytest -n 4)"']:
            with self.subTest(cmd=cmd):
                self.silent(cmd)

    def test_r1_directory_at_memrun_path_is_not_a_gate(self):
        d = self.tmp / "memrun_dir"
        d.mkdir(exist_ok=True)
        c = ctx(self.fires("pytest -n 4", dict(self.env, ENGRAM_MEMRUN_PATH=str(d))))
        self.assertNotIn("--need", c)
        self.assertIn("no host gate installed here", c)

    def test_r2_length_cap(self):
        import time
        big = "pytest -n 4 " + "x" * 250_000
        t = time.monotonic()
        self.silent(big)
        self.assertLess(time.monotonic() - t, 1.0)
        self.fires("pytest -n 4 " + "x" * 150_000)


if __name__ == "__main__":
    unittest.main()
