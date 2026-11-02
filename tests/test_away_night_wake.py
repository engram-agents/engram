"""Contract-first tests for tools/away_night_wake.py (away-night morning wake).

The tool DECIDES whether the engram-sleep skill should arm a one-shot
``CronCreate`` job for the next morning; it never calls CronCreate itself.
Cells 1-5 and 7 of the design are tested against the tool; cell 6
(CronCreate unavailable) is a skill-text guard because only the skill can
know it.  The charge-resolution cells compare the tool's bytes against the
runner's own ``_resolve_charge`` (extracted with sed at test time, never
hand-copied) so the two cannot drift silently.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "away_night_wake.py"
RUNNER = REPO / "deploy" / "autostart" / "agent-session-run.sh"
SKILL = REPO / "src" / "engram" / "skills" / "claude" / "engram-sleep" / "SKILL.md"
TIERS = REPO / "src" / "build" / "packaging" / "tiers.json"

PREFIX = "AWAY-NIGHT MORNING WAKE: the host was not rebooted; run this boot charge now.\n"
CHARGE = "Boot charge line one — café\r\nsecond line, no trailing newline"
PLACEHOLDER = "{{HARNESS_BOOT_INSTRUCTIONS}}"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class Env:
    def __init__(self, tmp: Path):
        self.shared = tmp / "shared"
        self.home = tmp / "home"
        self.eng = self.home / ".engram"
        self.charge_dir = tmp / "charges"
        self.eng.mkdir(parents=True)
        self.charge_dir.mkdir()

    def flag(self, obj=None, raw=None):
        self.shared.mkdir(exist_ok=True)
        p = self.shared / "away-night.json"
        if raw is not None:
            p.write_bytes(raw)
        else:
            p.write_text(json.dumps(obj))
        return p

    def good_flag(self, **kw):
        d = {"night_of": "2026-11-06", "wake_at": "08:15", "set_by": "commander"}
        d.update(kw)
        return self.flag(d)

    def override(self, text=CHARGE):
        (self.eng / "boot-charge-claude.md").write_bytes(text.encode("utf-8"))

    def legacy(self, text):
        (self.eng / "boot-charge.md").write_bytes(text.encode("utf-8"))

    def profile(self, text):
        (self.eng / "boot-charge-profile").write_text(text)

    def template(self, profile="default", base=None, comp=None):
        base = f"head\n{PLACEHOLDER}\ntail\n" if base is None else base
        comp = "COMPONENT-" + profile + "\n" if comp is None else comp
        (self.charge_dir / f"boot-charge-{profile}.md").write_bytes(base.encode("utf-8"))
        (self.charge_dir / f"boot-charge-{profile}-claude.md").write_bytes(comp.encode("utf-8"))

    def run(self, now="2026-11-06T22:00", platform=None):
        cmd = [sys.executable, str(TOOL), "--shared-dir", str(self.shared),
               "--engram-home", str(self.eng), "--charge-dir", str(self.charge_dir),
               "--now", now]
        if platform:
            cmd += ["--platform", platform]
        p = subprocess.run(cmd, capture_output=True)
        out = p.stdout.decode("utf-8")
        assert out.count("\n") <= 1, out
        data = json.loads(out)  # exactly one JSON object
        assert "Traceback" not in p.stderr.decode("utf-8", "replace")
        return p.returncode, data


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def assert_none(rc, d):
    assert rc == 0
    assert d == {"status": "none", "cron": None, "prompt": None, "fire_at": None, "final_line": ""}


def assert_not_armed(rc, d, night_of, tonight):
    """A VALID flag for another night: still `none`, but with the informational final line (v1.3)."""
    assert rc == 0
    assert d == {"status": "none", "cron": None, "prompt": None, "fire_at": None,
                 "final_line": "away-night flag is for %s, tonight is %s: not armed" % (night_of, tonight)}


def assert_error(rc, d, needle=None):
    assert rc == 2
    assert d["status"] == "error"
    assert d["final_line"].startswith("AWAY-NIGHT WAKE NOT ARMED:")
    assert d["cron"] is None and d["prompt"] is None
    if needle:
        assert needle in d["final_line"]


# ---- cell 1: no flag -------------------------------------------------------
def test_no_shared_dir(env):
    assert_none(*env.run())


def test_shared_dir_without_file(env):
    env.shared.mkdir()
    assert_none(*env.run())


# ---- cell 2: flag for tonight ---------------------------------------------
def test_arm_evening(env):
    env.good_flag()
    env.override()
    rc, d = env.run(now="2026-11-06T22:00")
    assert rc == 0 and d["status"] == "arm"
    assert d["fire_at"] == "2026-11-07T08:15"
    assert d["cron"] == "15 8 7 11 *"
    assert d["prompt"] == PREFIX + CHARGE  # byte-for-byte, no strip, no added newline
    assert d["final_line"] == "Away-night wake: one-shot at 2026-11-07T08:15 local (set_by commander)."


def test_arm_evening_wake_later_than_now_is_still_tomorrow(env):
    # discriminates "next occurrence of wake_at" (would be tonight 23:30) from
    # "the morning after the away night" (tomorrow 23:30)
    env.flag({"night_of": "2026-11-06", "wake_at": "23:30", "set_by": "commander"})
    env.override()
    rc, d = env.run(now="2026-11-06T22:00")
    assert rc == 0 and d["status"] == "arm"
    assert d["fire_at"] == "2026-11-07T23:30"
    assert d["cron"] == "30 23 7 11 *"


def _run_env_home(env, home_value):
    """Run WITHOUT --engram-home so the tool resolves ENGRAM_HOME itself (the pinned resolver)."""
    cmd = [sys.executable, str(TOOL), "--shared-dir", str(env.shared),
           "--charge-dir", str(env.charge_dir), "--now", "2026-11-06T22:00"]
    child_env = dict(os.environ, ENGRAM_HOME=home_value)
    p = subprocess.run(cmd, capture_output=True, env=child_env)
    return p.returncode, json.loads(p.stdout.decode("utf-8"))


def test_default_home_follows_engram_home_env(env):
    env.good_flag()
    env.override()
    rc, d = _run_env_home(env, str(env.eng))
    assert rc == 0 and d["status"] == "arm"
    assert d["prompt"] == PREFIX + CHARGE


def test_relative_engram_home_is_refused_loudly(env):
    env.good_flag()
    rc, d = _run_env_home(env, "relative/home")
    assert rc == 2 and d["status"] == "error"
    assert d["final_line"].startswith("AWAY-NIGHT WAKE NOT ARMED:")
    assert "absolute" in d["final_line"]


def test_set_by_unknown(env):
    env.flag({"night_of": "2026-11-06", "wake_at": "08:15"})
    env.override()
    rc, d = env.run()
    assert d["final_line"].endswith("(set_by unknown).")


def test_extra_keys_ignored(env):
    env.good_flag(lei_word="go", whatever=[1, 2])
    env.override()
    assert env.run()[1]["status"] == "arm"


def test_cron_unpadded_and_month_rollover(env):
    env.flag({"night_of": "2026-12-31", "wake_at": "07:05"})
    env.override()
    rc, d = env.run(now="2026-12-31T23:59")
    assert d["fire_at"] == "2027-01-01T07:05"
    assert d["cron"] == "5 7 1 1 *"


def test_cron_double_digit_fields(env):
    env.flag({"night_of": "2026-11-20", "wake_at": "13:45"})
    env.override()
    assert env.run(now="2026-11-20T21:00")[1]["cron"] == "45 13 21 11 *"


def test_cron_midnight_zero_fields(env):
    env.flag({"night_of": "2026-11-06", "wake_at": "00:00"})
    env.override()
    assert env.run(now="2026-11-06T22:00")[1]["cron"] == "0 0 7 11 *"


def test_0600_exactly_is_not_before_0600(env):
    env.good_flag()
    env.override()
    rc, d = env.run(now="2026-11-06T06:00")
    assert d["fire_at"] == "2026-11-07T08:15"


# ---- cell 3: another night -------------------------------------------------
@pytest.mark.parametrize("night", ["2026-11-05", "2026-11-07", "2020-01-01", "2030-01-01"])
def test_other_night_none(env, night):
    env.flag({"night_of": night, "wake_at": "08:15"})
    env.override()
    assert_not_armed(*env.run(now="2026-11-06T22:00"), night, "2026-11-06")


def test_yesterday_not_accepted_from_0600(env):
    env.flag({"night_of": "2026-11-05", "wake_at": "08:15"})
    env.override()
    assert_not_armed(*env.run(now="2026-11-06T06:00"), "2026-11-05", "2026-11-06")


def test_absent_flag_stays_silent_but_present_non_matching_flag_does_not(env):
    # discriminates "final_line empty only when the flag is ABSENT" from
    # "final_line empty whenever status is none"
    rc, absent = env.run(now="2026-11-06T22:00")  # no shared dir at all
    assert_none(rc, absent)
    env.flag({"night_of": "2026-11-05", "wake_at": "08:15"})
    rc, present = env.run(now="2026-11-06T22:00")
    assert present["status"] == "none" and present["final_line"] != ""
    assert_not_armed(rc, present, "2026-11-05", "2026-11-06")


def test_shared_path_not_a_directory_is_silent(env):
    env.shared.write_text("a file, not a directory")
    assert_none(*env.run())


# ---- cell 4: after-midnight sleep -----------------------------------------
def test_after_midnight_sleep(env):
    env.good_flag()
    env.override()
    rc, d = env.run(now="2026-11-07T00:30")
    assert rc == 0 and d["status"] == "arm"
    assert d["fire_at"] == "2026-11-07T08:15"
    assert d["cron"] == "15 8 7 11 *"


def test_after_midnight_does_not_accept_todays_date(env):
    env.flag({"night_of": "2026-11-07", "wake_at": "08:15"})
    env.override()
    # tonight is still 2026-11-06 (before 06:00); a flag dated the calendar date is for tomorrow night
    assert_not_armed(*env.run(now="2026-11-07T00:30"), "2026-11-07", "2026-11-06")


def test_after_midnight_0559_still_previous_night(env):
    env.flag({"night_of": "2026-11-06", "wake_at": "06:00"})
    env.override()
    rc, d = env.run(now="2026-11-07T05:59")
    assert d["status"] == "arm" and d["cron"] == "0 6 7 11 *"


@pytest.mark.parametrize("wake", ["00:30", "00:10"])
def test_after_midnight_wake_not_after_now_is_error(env, wake):
    env.flag({"night_of": "2026-11-06", "wake_at": wake})
    env.override()
    assert_error(*env.run(now="2026-11-07T00:30"), needle="wake_at already passed")


# ---- cell 5: malformed / unreadable flag ----------------------------------
@pytest.mark.parametrize("raw", [
    b"not json", b"", b"[1,2]", b'"s"', b"null",
    b'{"wake_at": "08:15"}', b'{"night_of": "2026-11-06"}',
])
def test_malformed_flag(env, raw):
    env.flag(raw=raw)
    env.override()
    assert_error(*env.run())


@pytest.mark.parametrize("night", ["2026-02-30", "2026-13-01", "26-11-06", "2026-1-6", "", 20261106, None, "2026-11-06x"])
def test_bad_night_of(env, night):
    env.flag({"night_of": night, "wake_at": "08:15"})
    env.override()
    assert_error(*env.run(), needle="night_of")


@pytest.mark.parametrize("wake", ["24:00", "08:60", "8:15", "0815", "", 815, None, "08:15:00", "ab:cd"])
def test_bad_wake_at(env, wake):
    env.flag({"night_of": "2026-11-06", "wake_at": wake})
    env.override()
    assert_error(*env.run(), needle="wake_at")


def test_malformed_flag_beats_other_night_none(env):
    # a malformed flag is loud even when its night_of would not match
    env.flag({"night_of": "2020-01-01", "wake_at": "99:99"})
    assert_error(*env.run())


@pytest.mark.skipif(IS_ROOT, reason="needs non-root")
def test_unreadable_flag(env):
    p = env.good_flag()
    p.chmod(0)
    try:
        assert_error(*env.run())
    finally:
        p.chmod(0o644)


def test_flag_is_directory(env):
    env.shared.mkdir()
    (env.shared / "away-night.json").mkdir()
    assert_error(*env.run())


# ---- platform --------------------------------------------------------------
def test_platform_other_than_claude(env):
    env.good_flag()
    env.override()
    rc, d = env.run(platform="codex")
    assert_error(rc, d)
    assert d["final_line"] == "AWAY-NIGHT WAKE NOT ARMED: platform codex is not covered by this wake path"


def test_platform_other_with_other_night_is_none_with_informational_line(env):
    env.flag({"night_of": "2020-01-01", "wake_at": "08:15"})
    assert_not_armed(*env.run(platform="codex"), "2020-01-01", "2026-11-06")


# ---- cell 7: charge resolution (tool-only cases) ---------------------------
def test_override_wins_over_legacy(env):
    env.good_flag()
    env.override("OVERRIDE")
    env.legacy("LEGACY")
    assert env.run()[1]["prompt"] == PREFIX + "OVERRIDE"


def test_legacy_used_when_no_override(env):
    env.good_flag()
    env.legacy("LEGACY\n")
    assert env.run()[1]["prompt"] == PREFIX + "LEGACY\n"


def test_trailing_newline_preserved_and_not_added(env):
    env.good_flag()
    env.override("with newline\n")
    assert env.run()[1]["prompt"] == PREFIX + "with newline\n"
    env.override("without")
    assert env.run()[1]["prompt"] == PREFIX + "without"


def test_no_charge_anywhere_is_error(env):
    env.good_flag()
    assert_error(*env.run())


def test_template_missing_component_is_error(env):
    env.good_flag()
    env.template()
    (env.charge_dir / "boot-charge-default-claude.md").unlink()
    assert_error(*env.run())


def test_template_empty_base_is_error(env):
    env.good_flag()
    env.template(base="")
    assert_error(*env.run())


def test_template_empty_component_is_error(env):
    env.good_flag()
    env.template(comp="")
    assert_error(*env.run())


def test_template_without_placeholder_is_error(env):
    env.good_flag()
    env.template(base="no placeholder here\n")
    assert_error(*env.run(), needle="placeholder")


def test_empty_override_is_error_like_runner(env):
    env.good_flag()
    env.override("")
    env.legacy("LEGACY")
    assert_error(*env.run())


# ---- cell 7 discriminating: compare against the runner's _resolve_charge ---
BASH = shutil.which("bash")


def _runner_fn() -> str:
    out = subprocess.run(["sed", "-n", "/^_resolve_charge()/,/^}/p", str(RUNNER)],
                         capture_output=True, check=True).stdout.decode("utf-8")
    assert out.startswith("_resolve_charge()") and out.rstrip().endswith("}")
    return out


def _runner_bytes(env: Env):
    """Run the runner's own function; return (rc, charge bytes or None)."""
    script = (f'HOME="$1"; CHARGE_DIR="$2"; _log_warn() {{ :; }}\n{_runner_fn()}\n'
              '_resolve_charge claude 2>/dev/null\n')
    p = subprocess.run([BASH, "-c", script, "bash", str(env.home), str(env.charge_dir)],
                       capture_output=True)
    if p.returncode != 0:
        return p.returncode, None
    path = p.stdout.decode("utf-8").rstrip("\n")
    return 0, Path(path).read_bytes()


def _tool_charge(env: Env):
    rc, d = env.run()
    if d["status"] != "arm":
        return rc, None
    assert d["prompt"].startswith(PREFIX)
    return 0, d["prompt"][len(PREFIX):].encode("utf-8")


def _both_templates(e):
    e.template("default")
    e.template("quiet")


def _case_quiet(e):
    _both_templates(e)
    e.profile("quiet\n")


def _case_spaces(e):
    _both_templates(e)
    e.profile("  quiet  \nsecond\n")


def _case_invalid(e):
    _both_templates(e)
    e.profile("loud\n")


def _case_empty_profile(e):
    _both_templates(e)
    e.profile("")


def _case_both(e):
    e.legacy("LEGACY")
    e.override("OVERRIDE no newline")


CASES = {
    "override": lambda e: e.override(CHARGE),
    "legacy": lambda e: e.legacy("legacy\r\ncharge no newline"),
    "override_and_legacy": _case_both,
    "template_default": lambda e: e.template("default"),
    "template_quiet": _case_quiet,
    "profile_with_spaces": _case_spaces,
    "profile_invalid_falls_to_default": _case_invalid,
    "profile_empty_falls_to_default": _case_empty_profile,
    "template_no_trailing_newline": lambda e: e.template(
        "default", base=f"a\r\n\n{PLACEHOLDER}\n  {PLACEHOLDER}\nlast é", comp="C no newline"),
    "template_placeholder_twice": lambda e: e.template(
        "default", base=f"{PLACEHOLDER}\nmid\n{PLACEHOLDER}\n", comp="X\n"),
    "template_no_placeholder": lambda e: e.template("default", base="nothing\n"),
    "nothing_at_all": lambda e: None,
}


@pytest.mark.skipif(BASH is None or not RUNNER.exists(), reason="bash or runner not available")
@pytest.mark.parametrize("case", sorted(CASES))
def test_charge_matches_runner(env, case):
    CASES[case](env)
    env.good_flag()
    r_rc, r_bytes = _runner_bytes(env)
    t_rc, t_bytes = _tool_charge(env)
    assert (r_rc == 0) == (t_rc == 0), (case, r_rc, t_rc)
    assert t_bytes == r_bytes


@pytest.mark.skipif(BASH is None or not RUNNER.exists() or IS_ROOT, reason="needs bash and non-root")
def test_unreadable_override_falls_through_like_runner(env):
    env.good_flag()
    env.override("OVERRIDE")
    env.legacy("LEGACY")
    ov = env.eng / "boot-charge-claude.md"
    ov.chmod(0)
    try:
        r_rc, r_bytes = _runner_bytes(env)
        t_rc, t_bytes = _tool_charge(env)
    finally:
        ov.chmod(0o644)
    assert r_bytes == b"LEGACY"
    assert t_bytes == r_bytes


# ---- defaults / robustness -------------------------------------------------
def test_engram_home_env_default(env, monkeypatch):
    env.good_flag()
    env.override()
    monkeypatch.setenv("ENGRAM_HOME", str(env.eng))
    cmd = [sys.executable, str(TOOL), "--shared-dir", str(env.shared),
           "--charge-dir", str(env.charge_dir), "--now", "2026-11-06T22:00"]
    p = subprocess.run(cmd, capture_output=True)
    assert json.loads(p.stdout)["status"] == "arm"


def test_bad_now_is_error_not_traceback(env):
    env.good_flag()
    env.override()
    assert_error(*env.run(now="garbage"))


def test_read_only_tool_writes_nothing(env):
    env.good_flag()
    env.template()

    def snap():
        return sorted(str(p) for p in list(env.home.rglob("*")) + list(env.charge_dir.rglob("*")))

    before = snap()
    env.run()
    assert snap() == before


# ---- cell 6 + skill/manifest guards ---------------------------------------
def _step() -> str:
    text = SKILL.read_text()
    m = re.search(r"^### Step 9\.75 — Arm the away-night morning wake \(LAST act\)\n(.*?)(?=^### |^---$|^## |\Z)",
                  text, re.S | re.M)
    assert m, "Step 9.75 heading missing"
    return m.group(1)


def test_skill_step_listed_at_top():
    head = SKILL.read_text().split("\nThe dream master owns the heavy", 1)[0]
    assert "Step 9.75" in head


def test_skill_step_runs_tool_in_guarded_form():
    s = _step()
    assert ('"$HOME/.engram/venv/bin/python3" "${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}'
            '/marketplace/plugins/engram}/tools/away_night_wake.py"') in s


def test_skill_none_bullet_pins_both_halves_of_final_line():
    s = _step()
    m = re.search(r"^- \*\*`none`\*\*[^\n]*$", s, re.M)
    assert m, "none bullet missing"
    b = m.group(0)
    assert "final_line" in b
    assert re.search(r"empty[^.\n]*no extra output", b)  # half 1: empty -> silent
    assert re.search(r"non-empty[^.\n]*final line", b)  # half 2: non-empty -> print as the final line


def test_skill_and_docstring_state_night_of_is_evening_date():
    s = _step()
    assert "EVENING date" in s
    assert "EVENING date" in TOOL.read_text().split('"""', 2)[1]


def test_skill_step_arm_instructions():
    s = _step()
    assert "CronCreate" in s and "recurring: false" in s and "CronList" in s
    assert "ScheduleWakeup" in s  # told NOT to also arm one
    assert "boot charge" in s and "loop-wake" in s


def test_skill_step_loud_failure_cell6():
    s = _step()
    assert "AWAY-NIGHT WAKE NOT ARMED:" in s
    assert re.search(r"CronCreate[^.\n]*(unavailable|refus)", s)
    assert re.search(r"never[^.\n]*silent", s, re.I)
    # the fourth path: the command fails before it prints JSON
    assert "tool did not run" in s
    # CronList must be read, and an absent job is a loud failure
    assert "job missing after CronCreate" in s
    # error exits 2 with JSON on stdout: normal path, not a crash
    assert re.search(r"exits 2[^.\n]*JSON", s)


def test_skill_step_is_after_95():
    text = SKILL.read_text()
    assert text.index("### Step 9.5") < text.index("### Step 9.75")


def test_tier_entry_present():
    data = json.loads(TIERS.read_text())
    assert '"path": "tools/away_night_wake.py"' in TIERS.read_text()
    assert isinstance(data, dict)
