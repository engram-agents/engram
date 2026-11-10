"""Contract tests: the autostart runner pins the Claude model FAMILY.

Cause (public issue 20): a seat came up on a model family other than the one
its recorded ``self_lineage`` names, and the model gate stopped it for hours.
The runner now passes ``--model <family>`` derived from ``self_lineage``.

Contract (stated before the code; ORDER matters):

A.  If the runner's environment has ANY non-empty ``ANTHROPIC_DEFAULT_*_MODEL``
    variable, pass no ``--model`` and stop (a seat that remaps models through
    the environment would otherwise get a literal id it cannot serve). An
    empty variable counts as unset.
A'. Provider other than ``anthropic`` (for example ``deepseek:flash``): no
    flag and exactly ONE INFO line; this wins over the settings comparison.
    Lineage unset, with no colon, or an unknown anthropic family: no flag and
    a loud WARNING; the seat starts as it did before.
B.  Anthropic lineage only: read ``model`` from the merged settings view (user,
    then project, then local; later wins), strip a trailing ``[...]``.
    Same family: no flag. Absent: ``--model <family>``. Different anthropic
    family: ``--model <lineage family>`` AND one loud disagreement line. A
    settings file that cannot be read: no flag and a loud line saying so (no
    approximation). A value that names no known family: the lineage flag and a
    loud line.

Always: a family word, never a model id; settings files are never edited;
``ANTHROPIC_API_KEY`` is never set and ``ANTHROPIC_BASE_URL`` is never
overwritten; the Codex path is untouched. No tmux, systemd, daemon or live
platform is started: only the external ``agentctl`` is replaced.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "deploy" / "autostart" / "agent-session-run.sh"
CHARGE_SOURCE = REPO_ROOT / "deploy" / "autostart"

# Variables stripped from the inherited environment so a seat running these
# tests cannot leak its own model remapping into a cell.
_SCRUB = [
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
]


class Run:
    def __init__(self, proc, argv, env_seen, home, cwd):
        self.proc = proc
        self.argv = argv
        self.env_seen = env_seen
        self.home = home
        self.cwd = cwd

    @property
    def log(self) -> str:
        p = self.home / ".engram" / "logs" / "autostart-launch.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def model_args(self) -> list[str]:
        out = []
        for i, a in enumerate(self.argv):
            if a == "--model" and i + 1 < len(self.argv):
                out.append(self.argv[i + 1])
            elif a.startswith("--model="):
                out.append(a.split("=", 1)[1])
        return out

    def lines(self, word: str) -> list[str]:
        return [ln for ln in self.proc.stderr.splitlines() if f"{word}" in ln]


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def run_runner(
    tmp_path: Path,
    *,
    platform: str = "claude",
    lineage: str | None = None,
    user_model: str | None = None,
    project_model: str | None = None,
    local_model: str | None = None,
    raw_user_settings: str | None = None,
    project_raw: str | None = None,
    lock_project_dir: bool = False,
    env_extra: dict[str, str] | None = None,
) -> Run:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "session-argv.nul"
    envdump = tmp_path / "env-seen.txt"
    charge_content = tmp_path / "selected-charge.md"
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    (home / ".claude").mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()

    if lineage is not None:
        _write_json(home / ".engram" / "config.json", {"self_lineage": lineage})
    else:
        _write_json(home / ".engram" / "config.json", {"agent_name": "mira"})
    if raw_user_settings is not None:
        _write_json(home / ".claude" / "settings.json", raw_user_settings)
    elif user_model is not None:
        _write_json(home / ".claude" / "settings.json", {"model": user_model})
    if project_model is not None:
        _write_json(cwd / ".claude" / "settings.json", {"model": project_model})
    if local_model is not None:
        _write_json(cwd / ".claude" / "settings.local.json", {"model": local_model})
    if project_raw is not None:
        _write_json(cwd / ".claude" / "settings.json", project_raw)

    agentctl = bin_dir / "agentctl"
    agentctl.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "if [[ \"${1:-}\" == __detect-platform-as-self ]]; then printf '%s\\n' \"$FAKE_PLATFORM\"; exit 0; fi\n"
        "if [[ \"${1:-}\" == session ]]; then\n"
        "  printf '%s\\0' \"$@\" > \"$FAKE_CALLS\"\n"
        "  env | grep -E '^ANTHROPIC_' > \"$FAKE_ENV\" || true\n"
        "  charge=\"${@: -1}\"\n"
        "  cp \"$charge\" \"$FAKE_CHARGE_CONTENT\"\n"
        "  exit 0\n"
        "fi\n"
        "printf 'unexpected agentctl invocation: %s\\n' \"$*\" >&2\n"
        "exit 99\n",
        encoding="utf-8",
    )
    agentctl.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_PLATFORM": platform,
            "FAKE_CALLS": str(calls),
            "FAKE_ENV": str(envdump),
            "FAKE_CHARGE_CONTENT": str(charge_content),
        }
    )
    env.update(env_extra or {})
    if lock_project_dir:
        (cwd / ".claude").mkdir(exist_ok=True)
        (cwd / ".claude").chmod(0o000)
    try:
        proc = subprocess.run(
            ["bash", str(RUNNER), "mira", str(CHARGE_SOURCE), str(cwd)],
            capture_output=True,
            text=True,
            env=env,
        )
    finally:
        if lock_project_dir:
            (cwd / ".claude").chmod(0o755)
    argv = calls.read_bytes().decode().split("\0")[:-1] if calls.exists() else []
    env_seen = envdump.read_text(encoding="utf-8") if envdump.exists() else ""
    return Run(proc, argv, env_seen, home, cwd)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- E: lineage family, no settings model -> the flag -----------------------

@pytest.mark.parametrize("family", ["opus", "sonnet", "haiku", "fable"])
def test_E_lineage_family_passed_when_settings_silent(tmp_path, family):
    r = run_runner(tmp_path, lineage=f"anthropic:{family}")
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == [family]
    assert r.argv[-2] == "--charge-file"  # the charge file stays the last argument
    assert not r.lines("WARNING")


def test_E_settings_file_without_model_key_counts_as_absent(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:opus", raw_user_settings='{"theme": "dark"}')
    assert r.model_args() == ["opus"]
    assert not r.lines("WARNING")


# --- F: same family in settings -> no flag ----------------------------------

@pytest.mark.parametrize(
    "value", ["opus", "opus[1m]", "claude-opus-4-8", "Opus", "claude-opus-5-5[1m]"]
)
def test_F_same_family_in_user_settings_no_flag(tmp_path, value):
    r = run_runner(tmp_path, lineage="anthropic:opus", user_model=value)
    assert r.model_args() == []
    assert not r.lines("WARNING")


def test_F_same_family_other_lineage(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:sonnet", user_model="sonnet")
    assert r.model_args() == []
    assert not r.lines("WARNING")


# --- G: different anthropic family -> flag + one loud line -------------------

@pytest.mark.parametrize(
    "lineage,settings,flag",
    [
        ("anthropic:opus", "fable", "opus"),
        ("anthropic:sonnet", "opus[1m]", "sonnet"),
        ("anthropic:haiku", "claude-sonnet-5-5", "haiku"),
    ],
)
def test_G_different_family_passes_lineage_flag_and_warns_once(tmp_path, lineage, settings, flag):
    r = run_runner(tmp_path, lineage=lineage, user_model=settings)
    assert r.model_args() == [flag]
    warns = r.lines("WARNING")
    assert len(warns) == 1, warns
    assert flag in warns[0] and settings.split("[")[0].replace("claude-", "").split("-")[0] in warns[0].lower()


# --- H: merged settings layers: local > project > user -----------------------

def test_H_project_overrides_user(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:sonnet", user_model="opus", project_model="sonnet")
    assert r.model_args() == []
    assert not r.lines("WARNING")


def test_H_local_overrides_project(tmp_path):
    r = run_runner(
        tmp_path,
        lineage="anthropic:sonnet",
        user_model="sonnet",
        project_model="sonnet",
        local_model="haiku",
    )
    assert r.model_args() == ["sonnet"]
    assert len(r.lines("WARNING")) == 1


def test_H_project_settings_alone_is_read(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:opus", project_model="sonnet")
    assert r.model_args() == ["opus"]
    assert len(r.lines("WARNING")) == 1


def test_H_user_agrees_but_local_absent_means_no_flag(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:opus", user_model="opus[1m]", project_model=None)
    assert r.model_args() == []


# --- A: environment remap wins over everything -------------------------------

@pytest.mark.parametrize(
    "var",
    [
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
    ],
)
def test_A_nonempty_default_model_env_means_no_flag(tmp_path, var):
    r = run_runner(
        tmp_path,
        lineage="anthropic:opus",
        user_model="sonnet",  # differs: would be flag + warning if order were wrong
        env_extra={var: "some-served-model"},
    )
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == []
    assert not r.lines("WARNING")


def test_A_env_wins_over_non_anthropic_info(tmp_path):
    r = run_runner(
        tmp_path,
        lineage="deepseek:flash",
        env_extra={"ANTHROPIC_DEFAULT_SONNET_MODEL": "served"},
    )
    assert r.model_args() == []
    assert not r.lines("WARNING")
    # the provider branch must not even have run: no line about the provider
    assert not [ln for ln in r.proc.stderr.splitlines() if "provider" in ln]


def test_A_env_wins_over_odd_lineage(tmp_path):
    r = run_runner(tmp_path, lineage="opus", env_extra={"ANTHROPIC_DEFAULT_OPUS_MODEL": "served"})
    assert r.model_args() == []
    assert not r.lines("WARNING")


@pytest.mark.parametrize("value", ["", ])
def test_A_empty_env_var_is_unset(tmp_path, value):
    r = run_runner(
        tmp_path,
        lineage="anthropic:opus",
        env_extra={"ANTHROPIC_DEFAULT_OPUS_MODEL": value},
    )
    assert r.model_args() == ["opus"]


# --- A': provider other than anthropic -> one INFO, no flag ------------------

def test_Aprime_non_anthropic_provider_no_flag_one_info(tmp_path):
    r = run_runner(tmp_path, lineage="deepseek:flash", user_model="opus")
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == []
    assert len(r.lines("INFO")) == 1, r.proc.stderr
    assert not r.lines("WARNING")
    # the INFO line reaches the log too, once (not only the pane)
    assert len([ln for ln in r.log.splitlines() if "INFO" in ln]) == 1, r.log


# --- A': unset / odd lineage -> loud, starts as today ------------------------

@pytest.mark.parametrize("lineage", [None, "", "opus", "anthropic:", ":opus", "anthropic:mistral"])
def test_odd_lineage_is_loud_and_starts_as_today(tmp_path, lineage):
    r = run_runner(tmp_path, lineage=lineage, user_model="opus")
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == []
    warns = r.lines("WARNING")
    assert len(warns) == 1, warns
    assert "model" in warns[0].lower()
    assert "WARNING" in r.log  # the log carries it too, not only the pane


# --- unreadable settings: say so, do not approximate -------------------------

def test_unreadable_settings_say_so_and_pass_no_flag(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:opus", raw_user_settings="{not json")
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == []
    warns = r.lines("WARNING")
    assert len(warns) == 1
    assert "settings" in warns[0].lower()


# --- unrecognised settings value ---------------------------------------------

def test_unrecognised_settings_value_passes_lineage_flag_and_warns(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:opus", user_model="opusplan")
    assert r.model_args() == ["opus"]
    assert len(r.lines("WARNING")) == 1


# --- invariants ---------------------------------------------------------------

def test_family_is_never_a_model_id(tmp_path):
    for i, fam in enumerate(("opus", "sonnet", "haiku", "fable")):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        r = run_runner(sub, lineage=f"anthropic:{fam}")
        (value,) = r.model_args()
        assert value == fam
        assert not re.search(r"claude|\d", value)


def test_settings_files_are_never_edited(tmp_path):
    r = run_runner(
        tmp_path,
        lineage="anthropic:opus",
        user_model="sonnet",
        project_model="haiku",
        local_model="fable",
    )
    assert r.model_args() == ["opus"]
    for p in (
        r.home / ".claude" / "settings.json",
        r.cwd / ".claude" / "settings.json",
        r.cwd / ".claude" / "settings.local.json",
    ):
        assert json.loads(p.read_text())["model"] in {"sonnet", "haiku", "fable"}
    assert json.loads((r.home / ".claude" / "settings.json").read_text()) == {"model": "sonnet"}


def test_credentials_and_base_url_untouched(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:opus", env_extra={"ANTHROPIC_BASE_URL": "http://127.0.0.1:9/served"})
    assert r.model_args() == ["opus"]
    assert "ANTHROPIC_API_KEY" not in r.env_seen
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:9/served" in r.env_seen


def test_exactly_one_model_flag(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:haiku", user_model="opus")
    assert len(r.model_args()) == 1
    assert r.argv.count("--model") == 1


def test_log_records_the_launch_with_the_flag(tmp_path):
    r = run_runner(tmp_path, lineage="anthropic:fable")
    assert "--model fable" in r.log


# --- codex path untouched -----------------------------------------------------

@pytest.mark.parametrize("lineage", ["anthropic:opus", "openai:codex", None, "opus"])
def test_codex_path_has_no_model_flag_and_no_model_warning(tmp_path, lineage):
    r = run_runner(tmp_path, platform="codex", lineage=lineage, user_model="opus")
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == []
    assert not [ln for ln in r.lines("WARNING") if "model" in ln.lower()]
    assert not r.lines("INFO")
    assert "-r" not in r.argv


@pytest.mark.parametrize("settings", [None, "sonnet"])
def test_codex_path_ignores_lineage_whether_settings_agree_or_not(tmp_path, settings):
    # the settings value must vary: with settings that agree with the lineage a
    # pin applied to Codex by mistake would also pass no flag (held-constant trap)
    r = run_runner(tmp_path, platform="codex", lineage="anthropic:opus", user_model=settings)
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == []
    assert not r.lines("WARNING")


# --- folded from reviewer round 1 --------------------------------------------

def test_keyless_later_layer_does_not_erase_an_earlier_model(tmp_path):
    # user says opus, project file exists but has no model key: the user value
    # must still be compared (opus vs lineage sonnet: flag + one warning)
    r = run_runner(tmp_path, lineage="anthropic:sonnet", user_model="opus", project_raw='{"theme": "x"}')
    assert r.model_args() == ["sonnet"]
    assert len(r.lines("WARNING")) == 1


def test_every_default_model_variable_is_checked_not_just_the_first(tmp_path):
    r = run_runner(
        tmp_path,
        lineage="anthropic:opus",
        env_extra={
            # the one non-empty variable sorts LAST, so a loop that looks only at
            # the first variable would see an empty one and wrongly pass a flag
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "served",
        },
    )
    assert r.model_args() == []
    assert not r.lines("WARNING")


def test_unreadable_project_settings_dir_warns_instead_of_dying_silently(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    r = run_runner(tmp_path, lineage="anthropic:opus", user_model="opus", lock_project_dir=True)
    assert r.proc.returncode == 0, r.proc.stderr
    assert r.model_args() == []
    warns = r.lines("WARNING")
    assert len(warns) == 1, r.proc.stderr
    assert "cannot read settings" in warns[0].lower()  # not the catch-all's wording
    assert "Traceback" not in r.proc.stderr
    assert "WARNING" in r.log


def test_codex_argv_is_exactly_the_pre_pin_shape(tmp_path):
    r = run_runner(tmp_path, platform="codex", lineage="anthropic:opus", user_model="sonnet")
    assert r.argv[:6] == ["session", "mira", "-d", "--as-self", "--cd", str(r.cwd)]
    assert r.argv[6] == "--charge-file" and len(r.argv) == 8
