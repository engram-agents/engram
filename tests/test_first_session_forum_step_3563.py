"""Contract-first tests for the first-session forum step (part of #3563, slice 2a).

Since #3550 / #3552 / #3559 "forum configured" means ``config.json``
``forum.url`` is a non-empty string and no gate infers it. The only per-seat
path that can ask a human for a LAN address is the first-session skill, so it
gets one new step, ``Step 5.7 -- Forum connection (optional)``, that writes the
key ONLY through ``src/engram/tools/set_forum_url.py``.

A skill is prose an agent follows, so this file has two halves and they prove
different things:

  C7  EXECUTABLE half. The bash block in Step 5.7 (the fence whose first line
      is the ``# forum-url-step`` marker) is extracted from the SKILL.md and RUN
      for real against a temp ENGRAM_HOME, a temp HOME and a fake
      CLAUDE_PLUGIN_ROOT holding a symlink to the real tool. It proves the
      block and the tool agree on exit codes and that the block never writes
      the file itself.
  C8  PINNED-WORDING half. Lint cells that read the SKILL.md text. They prove
      only that the instruction stays WRITTEN (heading position, the three
      answers, the no-inline-write rule, the --force and never-guess
      instructions). They cannot prove an agent follows it.

Block contract (#3571): the answer arrives as DATA in the file
``$ENGRAM_HOME/forum-url.answer`` (one line, written with a file-writing tool;
absent = no answer yet). There is no template fence and no ``FORUM_ANSWER``
environment variable. The cells here write that file with a plain
``open().write()`` (what the file tool does: data, no shell). With no answer and a non-empty string
``forum.url`` present, the block prints the value and says to skip the
question (nothing is written). With no answer and none set, it says the key is
not set (ask the question). With an answer, it runs the tool, shows its output
verbatim and prints ``set_forum_url.py exit status: N``. A missing tool prints
``ERROR: cannot locate set_forum_url.py``. The block's own exit status is
always 0.

Every cell is red on a dev without the step (the heading and fence do not
exist).
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "src" / "engram" / "skills" / "claude" / "engram-first-session" / "SKILL.md"
TOOL = REPO / "src" / "engram" / "tools" / "set_forum_url.py"

MARKER = "# forum-url-step"
LOCAL = "http://localhost:5002"
LAN = "http://192.168.1.50:5002"
SKIP_LINE = ("set_forum_url.py is not installed here "
             "(no forum components on this install): skip this step")

# Every subprocess gets its environment from _hermetic_env: HOME and
# XDG_CONFIG_HOME pinned to one temp dir, any inherited ENGRAM_HOME dropped, so
# the cells do not depend on the seat they run on (a forum-admin seat's
# ~/.config/systemd/user/engram-forum.service, a real ~/.engram, ...).
_HERMETIC_HOME = tempfile.mkdtemp(prefix="first-session-forum-home-")
atexit.register(shutil.rmtree, _HERMETIC_HOME, ignore_errors=True)


def _hermetic_env(**extra):
    env = dict(os.environ)
    env.pop("ENGRAM_HOME", None)
    env.pop("FORUM_URL", None)
    env.pop("FORUM_ANSWER", None)
    env.pop("FORUM_FORCE", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["HOME"] = _HERMETIC_HOME
    env["XDG_CONFIG_HOME"] = os.path.join(_HERMETIC_HOME, ".config")
    env.update(extra)
    return env


def _skill_text():
    return SKILL.read_text(encoding="utf-8")


def _headings(text):
    return re.findall(r"^## (.+)$", text, flags=re.MULTILINE)


def _step57_section(text):
    m = re.search(r"^## Step 5\.7 .*?(?=^## )", text, flags=re.MULTILINE | re.DOTALL)
    assert m, "no '## Step 5.7' section followed by another '## ' heading"
    return m.group(0)


def _fences(section):
    return re.findall(r"^```bash\n(.*?)^```", section, flags=re.MULTILINE | re.DOTALL)


def _block():
    """The one bash fence carrying the marker, inside the Step 5.7 section."""
    fences = [f for f in _fences(_step57_section(_skill_text())) if MARKER in f]
    assert len(fences) == 1, f"expected exactly one marked fence, found {len(fences)}"
    return fences[0]


def _plugin_root(tmp_path):
    root = tmp_path / "plugin-root"
    (root / "tools").mkdir(parents=True)
    os.symlink(TOOL, root / "tools" / "set_forum_url.py")
    return root


def _run(tmp_path, config=None, answer=None, plugin_root="fake", extra_env=None,
         stderr_to_devnull=False):
    """Run the extracted block; return (proc, config_path, bytes_before)."""
    home = tmp_path / "engram-home"
    home.mkdir(exist_ok=True)
    cfg = home / "config.json"
    before = None
    if config is not None:
        cfg.write_text(config if isinstance(config, str) else json.dumps(config, indent=2) + "\n")
        before = cfg.read_bytes()
    env = {"ENGRAM_HOME": str(home)}
    if plugin_root == "fake":
        env["CLAUDE_PLUGIN_ROOT"] = str(_plugin_root(tmp_path))
    elif plugin_root == "empty":
        empty = tmp_path / "empty-root"
        empty.mkdir()
        env["CLAUDE_PLUGIN_ROOT"] = str(empty)
    if answer:
        # #3571: the answer is a file the agent writes (data), not an environment variable.
        (home / "forum-url.answer").write_text(answer + "\n")
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", "-c", _block()], env=_hermetic_env(**env),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if stderr_to_devnull else subprocess.PIPE,
        text=True, timeout=60,
    )
    return proc, cfg, before


FORCE_MARKER = "# forum-url-force"


def _force_fence():
    """The force fence (first line ``# forum-url-force``)."""
    fences = [f for f in _fences(_step57_section(_skill_text())) if FORCE_MARKER in f]
    assert len(fences) == 1, f"expected exactly one force fence, found {len(fences)}"
    return fences[0]


def _run_as_instructed(tmp_path, url, config, force=None):
    """Run EXACTLY what the skill tells an agent to do (#3571): write the user's URL
    to ``$ENGRAM_HOME/forum-url.answer`` the way a file tool does (a plain
    ``open().write()``, no shell), then run (only if ``force`` is set) the force
    fence, then the block, as ONE ``bash -c`` script. ``force="fence"`` pastes the
    skill's own force fence; any other string is pasted as
    ``export FORUM_FORCE=<string>``. The URL never enters the script text."""
    script = ""
    if force == "fence":
        script += _force_fence() + "\n"
    elif force is not None:
        script += f"export FORUM_FORCE={force}\n"
    script += _block()
    home = tmp_path / "engram-home"
    home.mkdir()
    cfg = home / "config.json"
    cfg.write_text(json.dumps(config, indent=2) + "\n")
    before = cfg.read_bytes()
    with open(home / "forum-url.answer", "wb") as f:
        f.write(url.encode("utf-8") + b"\n")
    env = _hermetic_env(ENGRAM_HOME=str(home), CLAUDE_PLUGIN_ROOT=str(_plugin_root(tmp_path)))
    proc = subprocess.run(["bash", "-c", script], env=env, cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=60)
    return proc, cfg, before


BASE = {"mode": "single", "self_lineage": "anthropic:opus"}


def _url(cfg_path):
    return json.loads(cfg_path.read_text()).get("forum", {}).get("url")


# --------------------------------------------------------------------------
# C7 -- the extracted block, run for real
# --------------------------------------------------------------------------

class TestC7BlockRuns:
    def test_answer_a_local_writes_default_port(self, tmp_path):
        proc, cfg, _ = _run(tmp_path, BASE, answer=LOCAL)
        assert proc.returncode == 0, proc.stderr
        assert _url(cfg) == LOCAL
        assert "written: forum.url set to " + LOCAL in proc.stdout
        assert "exit status: 0" in proc.stdout

    def test_answer_b_lan_url_writes_it(self, tmp_path):
        proc, cfg, _ = _run(tmp_path, BASE, answer=LAN)
        assert proc.returncode == 0, proc.stderr
        assert _url(cfg) == LAN
        # other keys survive: only forum.url was written
        assert json.loads(cfg.read_text())["self_lineage"] == "anthropic:opus"

    def test_answer_c_no_forum_leaves_file_byte_identical(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE, answer="")
        assert proc.returncode == 0, proc.stderr
        assert cfg.read_bytes() == before
        assert "forum" not in json.loads(cfg.read_text())

    def test_unset_answer_unset_key_says_not_set_and_writes_nothing(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE)
        assert proc.returncode == 0, proc.stderr
        assert cfg.read_bytes() == before
        assert "not set" in proc.stdout

    def test_forum_url_env_var_is_never_used(self, tmp_path):
        # answer (c) with FORUM_URL in the environment: must stay absent
        proc, cfg, before = _run(
            tmp_path, BASE, answer="", extra_env={"FORUM_URL": "http://env.example:5002"})
        assert proc.returncode == 0, proc.stderr
        assert cfg.read_bytes() == before

    def test_forum_url_env_var_with_no_answer_is_never_used(self, tmp_path):
        proc, cfg, before = _run(
            tmp_path, BASE, extra_env={"FORUM_URL": "http://env.example:5002"})
        assert proc.returncode == 0, proc.stderr
        assert cfg.read_bytes() == before

    def test_existing_equal_url_skips_question_and_writes_nothing(self, tmp_path):
        cfg_in = dict(BASE, forum={"url": LOCAL})
        proc, cfg, before = _run(tmp_path, cfg_in)
        assert proc.returncode == 0, proc.stderr
        assert cfg.read_bytes() == before
        assert LOCAL in proc.stdout
        assert "already set" in proc.stdout
        assert "not set" not in proc.stdout

    def test_existing_different_url_without_answer_is_reported_not_changed(self, tmp_path):
        cfg_in = dict(BASE, forum={"url": LAN})
        proc, cfg, before = _run(tmp_path, cfg_in)
        assert proc.returncode == 0, proc.stderr
        assert cfg.read_bytes() == before
        assert LAN in proc.stdout and "already set" in proc.stdout

    def test_existing_different_url_with_answer_reports_exit_3_untouched(self, tmp_path):
        cfg_in = dict(BASE, forum={"url": LAN})
        proc, cfg, before = _run(tmp_path, cfg_in, answer=LOCAL)
        assert proc.returncode == 0, proc.stderr  # non-fatal
        assert cfg.read_bytes() == before
        assert "exit status: 3" in proc.stdout
        assert "pass --force" in (proc.stdout + proc.stderr)  # the tool's own words, verbatim

    def test_bad_url_reports_exit_2_untouched_and_block_exits_0(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE, answer="http://h:0")
        assert proc.returncode == 0, proc.stderr
        assert cfg.read_bytes() == before
        assert "exit status: 2" in proc.stdout
        assert "refused:" in (proc.stdout + proc.stderr)

    def test_missing_config_reports_exit_2_and_creates_nothing(self, tmp_path):
        proc, cfg, _ = _run(tmp_path, None, answer=LOCAL)
        assert proc.returncode == 0, proc.stderr
        assert not cfg.exists()
        assert "exit status: 2" in proc.stdout

    def test_tool_missing_prints_error_and_exits_0(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE, answer=LOCAL, plugin_root="empty")
        assert proc.returncode == 0, proc.stderr
        assert "ERROR: cannot locate set_forum_url.py" in proc.stdout
        assert cfg.read_bytes() == before

    def test_plugin_root_unset_and_no_fallback_prints_error_and_exits_0(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE, answer=LOCAL, plugin_root=None)
        assert proc.returncode == 0, proc.stderr
        assert "ERROR: cannot locate set_forum_url.py" in proc.stdout
        assert cfg.read_bytes() == before

    def test_marketplace_fallback_path_is_used_when_plugin_root_is_unset(self, tmp_path):
        # The fallback is the runtime location under the ENGRAM home (#3863 6c): with
        # ENGRAM_HOME set (as _run does) the fake tool lives under that home, and the
        # hermetic HOME holds none.
        tools = tmp_path / "engram-home" / "marketplace" / "plugins" / "engram" / "tools"
        tools.mkdir(parents=True)
        os.symlink(TOOL, tools / "set_forum_url.py")
        proc, cfg, _ = _run(tmp_path, BASE, answer=LOCAL, plugin_root=None)
        assert proc.returncode == 0, proc.stderr
        assert _url(cfg) == LOCAL

    def test_marketplace_fallback_defaults_to_home_dot_engram_when_engram_home_is_unset(self, tmp_path):
        fake_home = tmp_path / "home"
        home = fake_home / ".engram"
        tools = home / "marketplace" / "plugins" / "engram" / "tools"
        tools.mkdir(parents=True)
        os.symlink(TOOL, tools / "set_forum_url.py")
        cfg = home / "config.json"
        cfg.write_text(json.dumps(BASE, indent=2) + "\n")
        (home / "forum-url.answer").write_text(LOCAL + "\n")
        env = _hermetic_env(HOME=str(fake_home))  # ENGRAM_HOME and CLAUDE_PLUGIN_ROOT dropped
        proc = subprocess.run(["bash", "-c", _block()], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert _url(cfg) == LOCAL

    # -- the invocation exactly as the skill prints it (template + block, one bash -c)

    def test_instructed_run_plain_url_is_written(self, tmp_path):
        proc, cfg, _ = _run_as_instructed(tmp_path, LOCAL, BASE)
        assert proc.returncode == 0, proc.stderr
        assert _url(cfg) == LOCAL, proc.stdout + proc.stderr
        assert "exit status: 0" in proc.stdout

    def test_instructed_run_url_with_apostrophe_is_written(self, tmp_path):
        url = "http://h:5002/a'b"
        proc, cfg, _ = _run_as_instructed(tmp_path, url, BASE)
        assert proc.returncode == 0, proc.stderr
        assert _url(cfg) == url, proc.stdout + proc.stderr

    @pytest.mark.parametrize("name,url", [
        ("dollar-paren", "http://h:5002/$(touch SENTINEL)"),
        ("backtick", "http://h:5002/`touch SENTINEL`"),
        ("quote-break", "http://h:5002/';touch SENTINEL;'"),
    ])
    def test_instructed_run_never_executes_the_url(self, tmp_path, name, url):
        sentinel = tmp_path / "SENTINEL"
        proc, cfg, before = _run_as_instructed(
            tmp_path, url.replace("SENTINEL", str(sentinel)), BASE)
        assert proc.returncode == 0, proc.stderr
        assert not sentinel.exists(), "the URL was executed by the shell"
        # the tool may refuse (file untouched) or store it as inert data
        assert cfg.read_bytes() == before or _url(cfg) == url.replace("SENTINEL", str(sentinel))

    # -- F2: no tool and no answer = no forum components on this install

    def test_tool_absent_no_answer_prints_the_skip_line_only(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE, plugin_root="empty")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == SKIP_LINE
        assert cfg.read_bytes() == before

    def test_tool_absent_no_answer_even_with_existing_url_prints_the_skip_line_only(self, tmp_path):
        proc, cfg, before = _run(tmp_path, dict(BASE, forum={"url": LOCAL}), plugin_root=None)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == SKIP_LINE
        assert cfg.read_bytes() == before

    def test_tool_absent_with_answer_keeps_the_error_line(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE, answer=LOCAL, plugin_root="empty")
        assert proc.returncode == 0, proc.stderr
        assert "ERROR: cannot locate set_forum_url.py" in proc.stdout
        assert SKIP_LINE not in proc.stdout
        assert cfg.read_bytes() == before

    def test_tool_present_no_answer_still_prints_set_or_not_set(self, tmp_path):
        proc, _, _ = _run(tmp_path, BASE)
        assert "forum.url is not set" in proc.stdout and SKIP_LINE not in proc.stdout
        (tmp_path / "x").mkdir()
        proc, _, _ = _run(tmp_path / "x", dict(BASE, forum={"url": LOCAL}))
        assert "forum.url is already set" in proc.stdout and SKIP_LINE not in proc.stdout

    # -- F3: --force only through the force fence, only when FORUM_FORCE is 1

    def test_instructed_force_fence_overwrites_a_different_url(self, tmp_path):
        proc, cfg, _ = _run_as_instructed(
            tmp_path, LOCAL, dict(BASE, forum={"url": LAN}), force="fence")
        assert proc.returncode == 0, proc.stderr
        assert _url(cfg) == LOCAL, proc.stdout + proc.stderr
        assert "exit status: 0" in proc.stdout

    def test_instructed_without_force_fence_stays_exit_3_untouched(self, tmp_path):
        proc, cfg, before = _run_as_instructed(tmp_path, LOCAL, dict(BASE, forum={"url": LAN}))
        assert proc.returncode == 0, proc.stderr
        assert "exit status: 3" in proc.stdout
        assert cfg.read_bytes() == before

    @pytest.mark.parametrize("value", ["0", "''", "yes", "true", "11"])
    def test_instructed_force_values_other_than_1_do_not_force(self, tmp_path, value):
        proc, cfg, before = _run_as_instructed(
            tmp_path, LOCAL, dict(BASE, forum={"url": LAN}), force=value)
        assert proc.returncode == 0, proc.stderr
        assert "exit status: 3" in proc.stdout
        assert cfg.read_bytes() == before

    # -- F4: the refusal reason must be in the stream the agent reads (stdout)

    def test_bad_url_reason_is_in_stdout_even_with_stderr_discarded(self, tmp_path):
        proc, cfg, before = _run(tmp_path, BASE, answer="http://h:0", stderr_to_devnull=True)
        assert proc.returncode == 0
        assert "exit status: 2" in proc.stdout
        assert "refused: URL port must be 1..65535" in proc.stdout
        assert cfg.read_bytes() == before

    def test_hermetic_env_pins_home_and_drops_engram_home(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_HOME", "/nonexistent/leak")
        monkeypatch.setenv("HOME", "/nonexistent/realhome")
        env = _hermetic_env()
        assert "ENGRAM_HOME" not in env
        assert env["HOME"] == _HERMETIC_HOME
        assert env["XDG_CONFIG_HOME"].startswith(_HERMETIC_HOME)


# --------------------------------------------------------------------------
# C8 -- pinned wording (proves the instruction stays written, nothing more)
# --------------------------------------------------------------------------

ORIGINAL_HEADINGS = [
    "Step 0 — Detect bootstrap mode",
    "Step 0.5 — Run bootstrap.py (fresh-bootstrap mode only)",
    "Step 0.6 — Validate Python env (defense-in-depth)",
    "Mindset",
    "Step 1 — Introduce yourself and ENGRAM",
    "Step 1.5 — Confirm the model family + record lineage (before identity forms)",
    "Step 2 — Learn about the user",
    "Step 3 — The naming moment",
    "Step 4 — Explain what happens next",
    "Step 5 — Drowsiness ceiling configuration",
    "Step 5.5 — Keeping semantic recall warm (persistent service)",
    "Step 5.6 — Viz server as a persistent service (optional)",
    "Step 6 — Finalize (the substitution step)",
    "Step 7 — Tell the user what's next",
    "Step 7.5 — Off-disk graph backup (recommended)",
    "Anti-patterns",
]


class TestC8PinnedWording:
    def test_heading_exists_between_5_6_and_6(self):
        heads = _headings(_skill_text())
        new = [h for h in heads if h.startswith("Step 5.7")]
        assert new == ["Step 5.7 — Forum connection (optional)"]
        i = heads.index(new[0])
        assert heads[i - 1].startswith("Step 5.6")
        assert heads[i + 1].startswith("Step 6")

    def test_no_existing_step_renumbered_or_reworded(self):
        heads = [h for h in _headings(_skill_text()) if not h.startswith("Step 5.7")]
        assert heads == ORIGINAL_HEADINGS

    def test_three_answers_present(self):
        sec = _step57_section(_skill_text())
        assert "this host" in sec and LOCAL in sec and "5002" in sec  # (a) + default-port note
        assert "another host" in sec and "LAN" in sec                  # (b)
        assert re.search(r"no forum", sec, re.IGNORECASE)              # (c)
        assert re.search(r"stays? absent", sec)                        # (c) leaves the key absent
        assert re.search(r"[Ii]f the user is unsure, \(c\) is the default", sec)

    def test_writes_only_through_the_tool(self):
        text = _skill_text()
        assert "set_forum_url.py" in _step57_section(text)
        # no inline write of the key anywhere in the file
        assert 'cfg["forum"]' not in text
        assert "forum_url =" not in text
        assert "json.dump" not in _step57_section(text)

    def test_force_instruction(self):
        sec = _step57_section(_skill_text())
        assert re.search(r"[Dd]o not pass\s+`--force`\s+unless the user says so", sec)

    def test_never_guessed_never_from_forum_url_env(self):
        sec = _step57_section(_skill_text())
        assert "never guessed" in sec
        assert re.search(r"never taken from[^.]*`FORUM_URL`", sec)

    def test_asked_once_skip_instruction(self):
        sec = _step57_section(_skill_text())
        assert re.search(r"already set", sec) and re.search(r"skip", sec, re.IGNORECASE)

    def test_fence_is_the_one_c7_runs(self):
        sec = _step57_section(_skill_text())
        fences = _fences(sec)
        assert len(fences) == 3, ("Step 5.7 carries the force fence, the block and the "
                                  "hint-consume fence (#3571 removed the answer template)")
        block = _block()
        assert block in fences
        assert block.lstrip().startswith(MARKER)
        assert "set_forum_url.py" in block
        assert "exit 1" not in block  # a missing tool must not abort first-session

    def test_no_bash_c_wording_in_step(self):
        sec = _step57_section(_skill_text())
        assert "bash -c" not in sec
        assert "bash -c '<" not in sec

    def test_answer_a_names_default_port_and_asks_for_the_port(self):
        sec = _step57_section(_skill_text())
        bullet = next(l for l in sec.splitlines() if l.startswith("> - **(a)"))
        assert "5002 is the installer's default port" in bullet
        assert "if yours differs, tell me the port" in bullet
        assert "write the URL the user gave" in sec

    def test_force_fence_is_exactly_the_export_and_prose_gates_it(self):
        f = _force_fence()
        assert [l for l in f.splitlines() if l.strip()] == [FORCE_MARKER, "export FORUM_FORCE=1"]
        sec = _step57_section(_skill_text())
        prose = re.sub(r"^```.*?^```", "", sec, flags=re.MULTILINE | re.DOTALL)
        assert re.search(r"(insert|paste) this fence (right )?before the block", prose)
        assert re.search(r"ONLY after the user has explicitly asked to overwrite", prose)

    def test_skip_line_is_pinned_in_prose_and_block(self):
        sec = _step57_section(_skill_text())
        prose = re.sub(r"^```.*?^```", "", sec, flags=re.MULTILINE | re.DOTALL)
        assert SKIP_LINE in _block()
        assert SKIP_LINE in prose
        assert re.search(r"SKIP the question", prose)

    def test_tool_call_merges_stderr(self):
        assert '"$PY" "$TOOL" "$ANSWER" ${FORCE:+--force} 2>&1' in _block()

    def test_template_shape(self):
        # #3571: the answer template fence is gone. No fence in Step 5.7 carries a
        # placeholder, a URL_EOF delimiter or an answer variable; the only heredocs
        # are the block's own constant, quoted Python reads.
        for f in _fences(_step57_section(_skill_text())):
            assert not f.lstrip().startswith("# forum-url-answer")
            assert "<URL>" not in f and "URL_EOF" not in f
            assert "export FORUM_ANSWER" not in f
            for m in re.finditer(r"<<(-?)\s*(['\"]?)(\w+)\2", f):
                assert m.group(3) == "PYEOF" and m.group(2) == "'", m.group(0)

    def test_tool_resolution_uses_plugin_root_pattern(self):
        assert "${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/tools/set_forum_url.py}" in _block()
