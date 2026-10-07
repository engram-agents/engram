"""#3568 -- GitHub integration is an explicit server-side switch.

Contract-first: written BEFORE the implementation (run against unchanged dev
first; the red/green record is in the PR hand-off).

CONTRACT (restated; the code carries the same table in a module comment):

  C1  Resolution. ``FORUM_GITHUB_INTEGRATION`` + ``GH_TOKEN`` are read ONCE, at
      startup in ``main()``:
          on    + token     -> ON  (today's gate, byte for byte)
          on    + no token  -> startup REFUSES (stderr line, non-zero exit)
          off   + any       -> OFF
          unset + token     -> ON  (legacy installs keep their gate)
          unset + no token  -> LEGACY-CLOSED: not OFF. The server starts and behaves
                               exactly as before the switch existed: the service
                               identity is on with no token, so every GitHub-gated
                               transition REFUSES (fail-closed). The refusal says how
                               to opt out. OFF is reached only by an explicit ``off``.
          other value       -> startup REFUSES, naming the value
      One startup line names the mode and why. ``create_app(github_integration=)``
      injects the setting; None (the default) means "no service mode": today's
      behaviour, which keeps every existing test meaningful.
  C2  OFF: no ``gh`` subprocess on ANY route. advance (peer -> merge) skips the
      login-map and approval read and proceeds with ci/approval ``unknown``,
      ``warnings`` == [the plain message], and the turn log records the unknown
      verdicts. flip (both spellings): never a refusal for a GitHub reason. The
      board PR-state read: ``unknown`` + the off detail, no gh. Every NON-GitHub
      refusal is unchanged.
  C3  ON is unchanged (GitHub refusals still refuse).
  C4  The installer always renders an explicit ``Environment=FORUM_GITHUB_INTEGRATION=``
      line: ``on`` with --github-token-file, ``off`` without.
  C5  Client: ``baton flip`` to the maintainer with no ``gh`` on PATH warns and
      proceeds; the unqualified-anchor refusal is unchanged.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import json
import os
import subprocess
import sys

import pytest

from forum import board_projects
from forum import server as server_mod
from forum.tests.test_baton_advance_api import (
    World, AUTHOR, COLLEAGUE, OTHER, SEAT, REPO, ANCHOR, MAP,
)
from forum.tests.test_installer import _install, _noncomment, _rendered

OFF_WARNING = "GitHub integration is off; enable it to gate on CI and approvals"
OFF_DETAIL = "GitHub integration is off"
TOKEN = "fake-token-for-test-" + "0000"
PID = "PR-9001"
FLIP_NOTE = "(GitHub integration is off; CI and approval not verified)"
LEGACY_CLOSED_LINE = (
    "[forum] GitHub integration: ON, no token (switch unset and no GH_TOKEN \u2014 "
    "GitHub-gated transitions will REFUSE; set FORUM_GITHUB_INTEGRATION=off for advisory "
    "mode, or install a read-only token)"
)
OPT_OUT_HINT = ("set FORUM_GITHUB_INTEGRATION=off on the forum server for advisory mode, "
                "or install a read-only token")


@pytest.fixture(autouse=True)
def _reset_globals():
    gate = server_mod._load_baton_gate()
    assert gate is not None
    gate._service_identity_reset()
    board_projects.configure_gh_identity(None)
    yield
    gate._service_identity_reset()
    board_projects.configure_gh_identity(None)


class GhTripwire:
    """Records EVERY attempt to start a process. Not an exception-raiser: a call
    site that swallows exceptions would otherwise hide the spawn."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="tripwire")


def _arm_tripwire(monkeypatch):
    trip = GhTripwire()
    monkeypatch.setattr(subprocess, "run", trip)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: trip(a[0] if a else k.get("args")))
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: trip(a[0] if a else k.get("args")))
    monkeypatch.setattr(os, "system", lambda c: trip(c))
    return trip


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _set(world, mode):
    world.app.config["GITHUB_INTEGRATION"] = mode


# ---------------------------------------------------------------------------
# C1 -- resolution
# ---------------------------------------------------------------------------

class TestResolution:
    @pytest.mark.parametrize("env,token,want", [
        ({"FORUM_GITHUB_INTEGRATION": "on"}, True, "on"),
        ({"FORUM_GITHUB_INTEGRATION": "off"}, True, "off"),
        ({"FORUM_GITHUB_INTEGRATION": "off"}, False, "off"),
        ({}, True, "on"),
        ({}, False, "on"),
    ], ids=["on+token", "off+token", "off+none", "unset+token", "unset+none"])
    def test_table_rows(self, env, token, want):
        environ = dict(env)
        if token:
            environ["GH_TOKEN"] = TOKEN
        mode, why = server_mod._resolve_github_integration(environ)
        assert mode == want
        assert why and TOKEN not in why

    def test_on_without_token_refuses_naming_the_token_source(self):
        with pytest.raises(server_mod.GithubIntegrationError) as ei:
            server_mod._resolve_github_integration({"FORUM_GITHUB_INTEGRATION": "on"})
        msg = str(ei.value)
        assert "GH_TOKEN" in msg and "--github-token-file" in msg and "\n" not in msg

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_token_counts_as_absent(self, blank):
        # unset + (blank = absent) token is the legacy-closed row, never OFF
        assert server_mod._resolve_github_integration({"GH_TOKEN": blank})[0] == "on"
        with pytest.raises(server_mod.GithubIntegrationError):
            server_mod._resolve_github_integration(
                {"FORUM_GITHUB_INTEGRATION": "on", "GH_TOKEN": blank})

    @pytest.mark.parametrize("bad", ["yes", "true", "1", "onn", "of f"])
    def test_other_value_refuses_naming_the_value(self, bad):
        with pytest.raises(server_mod.GithubIntegrationError) as ei:
            server_mod._resolve_github_integration(
                {"FORUM_GITHUB_INTEGRATION": bad, "GH_TOKEN": TOKEN})
        assert repr(bad) in str(ei.value)

    def test_create_app_default_is_no_service_mode(self, tmp_path):
        app = server_mod.create_app(str(tmp_path / "f.db"), str(tmp_path / "a.jsonl"))
        assert app.config.get("GITHUB_INTEGRATION") is None

    @pytest.mark.parametrize("mode", ["on", "off"])
    def test_create_app_accepts_injected_setting(self, tmp_path, mode):
        app = server_mod.create_app(str(tmp_path / "f.db"), str(tmp_path / "a.jsonl"),
                                    github_integration=mode)
        assert app.config["GITHUB_INTEGRATION"] == mode

    def test_create_app_rejects_a_bad_setting(self, tmp_path):
        with pytest.raises(ValueError):
            server_mod.create_app(str(tmp_path / "f.db"), str(tmp_path / "a.jsonl"),
                                  github_integration="maybe")


class TestMainStartup:
    def _argv(self, tmp_path, monkeypatch, env, token):
        monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
        monkeypatch.delenv("FORUM_HOME", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("FORUM_GITHUB_INTEGRATION", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        if token:
            monkeypatch.setenv("GH_TOKEN", TOKEN)
        monkeypatch.setattr(sys, "argv", [
            "forum.server", "--db", str(tmp_path / "svc" / "forum.db"),
            "--audit", str(tmp_path / "svc" / "audit.jsonl")])
        booted = []
        monkeypatch.setattr(server_mod, "_run_boot_verify",
                            lambda *a, **k: booted.append(1) or {})
        ran = []
        monkeypatch.setattr(server_mod.Flask, "run", lambda self, *a, **k: ran.append(self))
        return booted, ran

    @pytest.mark.parametrize("env,token,want", [
        ({"FORUM_GITHUB_INTEGRATION": "on"}, True, "on"),
        ({"FORUM_GITHUB_INTEGRATION": "off"}, True, "off"),
        ({"FORUM_GITHUB_INTEGRATION": "off"}, False, "off"),
        ({}, True, "on"),
        ({}, False, "on"),
    ], ids=["on+token", "off+token", "off+none", "unset+token", "unset+none"])
    def test_main_resolves_and_injects_the_mode(self, tmp_path, monkeypatch, capsys, env, token, want):
        booted, ran = self._argv(tmp_path, monkeypatch, env, token)
        server_mod.main()
        assert len(ran) == 1
        assert ran[0].config["GITHUB_INTEGRATION"] == want
        err = capsys.readouterr().err
        lines = [ln for ln in err.splitlines() if "GitHub integration" in ln]
        assert len(lines) == 1, err
        assert want.upper() in lines[0]
        assert TOKEN not in err

    def test_main_off_with_a_token_never_hands_it_to_the_gate(self, tmp_path, monkeypatch):
        self._argv(tmp_path, monkeypatch, {"FORUM_GITHUB_INTEGRATION": "off"}, True)
        server_mod.main()
        gate = server_mod._load_baton_gate()
        assert gate._SERVICE_IDENTITY.token == ""
        assert "GH_TOKEN" not in os.environ

    def test_main_on_without_token_refuses_before_serving(self, tmp_path, monkeypatch, capsys):
        booted, ran = self._argv(tmp_path, monkeypatch, {"FORUM_GITHUB_INTEGRATION": "on"}, False)
        with pytest.raises(SystemExit) as ei:
            server_mod.main()
        assert ei.value.code == 78   # EX_CONFIG: systemd must not crash-loop on it
        assert ran == [] and booted == []
        err = capsys.readouterr().err
        assert len([ln for ln in err.splitlines() if "GH_TOKEN" in ln]) == 1, err
        assert not (tmp_path / "svc").exists(), "refusal must precede any data-dir write"

    def test_main_bad_value_refuses_naming_it(self, tmp_path, monkeypatch, capsys):
        booted, ran = self._argv(tmp_path, monkeypatch, {"FORUM_GITHUB_INTEGRATION": "maybe"}, True)
        with pytest.raises(SystemExit) as ei:
            server_mod.main()
        assert ei.value.code == 78
        assert ran == []
        assert "'maybe'" in capsys.readouterr().err

    def test_legacy_closed_startup_line_is_pinned(self, tmp_path, monkeypatch, capsys):
        self._argv(tmp_path, monkeypatch, {}, False)
        server_mod.main()
        lines = [ln for ln in capsys.readouterr().err.splitlines() if "GitHub integration" in ln]
        assert lines == [LEGACY_CLOSED_LINE]

    def test_unset_without_token_is_not_off_and_the_gate_has_no_identity(
            self, tmp_path, monkeypatch):
        """Through main(): the resolved mode is not OFF; the gate is in service mode
        with no token (so every gate refuses, as before the switch)."""
        booted, ran = self._argv(tmp_path, monkeypatch, {}, False)
        server_mod.main()
        app = ran[0]
        assert app.config["GITHUB_INTEGRATION"] != "off"
        assert server_mod._github_off(server_mod._login_map_cfg(app)) is False
        gate = server_mod._load_baton_gate()
        assert gate._SERVICE_IDENTITY.on is True and gate._SERVICE_IDENTITY.token == ""

    def test_explicit_off_does_not_carry_the_legacy_hint(self, tmp_path, monkeypatch, capsys):
        self._argv(tmp_path, monkeypatch, {"FORUM_GITHUB_INTEGRATION": "off"}, False)
        server_mod.main()
        err = capsys.readouterr().err
        assert "switch unset" not in err and "REFUSE" not in err
        assert "FORUM_GITHUB_INTEGRATION=off" in err

    def test_missing_hard_dep_at_boot_still_exits_2(self, tmp_path, monkeypatch, capsys):
        """Contract: only the GitHub-switch refusal moved to 78. A boot-verify hard-dep
        failure (possibly transient: mount, permissions) keeps exit 2, so systemd restarts."""
        real_boot_verify = server_mod._run_boot_verify
        booted, ran = self._argv(tmp_path, monkeypatch, {}, False)
        monkeypatch.setattr(server_mod, "_run_boot_verify", real_boot_verify)
        monkeypatch.setattr(server_mod, "probe_deps", lambda *a, **k: {
            "db": "missing", "_db_error": "synthetic", "audit_log": "ok",
            "engram_pkg": "ok", "embeddings": "ok"})
        with pytest.raises(SystemExit) as ei:
            server_mod.main()
        assert ei.value.code == 2
        assert ran == []


# ---------------------------------------------------------------------------
# C2 -- OFF behaviour
# ---------------------------------------------------------------------------

class TestOffAdvance:
    def test_advance_proceeds_with_unknown_verdicts_and_no_gh(self, world, monkeypatch):
        world.seed()
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = world.advance()
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["advanced"] is True and body["phase"] == "merge"
        assert body["ci"] == {"state": "unknown", "detail": OFF_DETAIL}
        assert body["approval"] == {"state": "unknown", "detail": OFF_DETAIL}
        assert body["warnings"] == [OFF_WARNING]
        assert world.fields()[0]["phase"] == "merge"
        assert trip.calls == []

    def test_turn_log_records_the_unknown_verdicts(self, world, monkeypatch):
        world.seed()
        _set(world, "off")
        _arm_tripwire(monkeypatch)
        assert world.advance().status_code == 201
        lines = world.log_lines("GitHub integration is off")
        assert len(lines) == 1, lines
        assert "ci" in lines[0] and "approval" in lines[0] and "unknown" in lines[0]

    def test_advance_needs_no_login_map(self, tmp_path, monkeypatch):
        w = World(tmp_path, monkeypatch, with_map=False)
        w.seed()
        _set(w, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = w.advance()
        assert resp.status_code == 201, resp.get_json()
        assert trip.calls == []

    def test_advance_needs_no_colleague_mapping(self, world, monkeypatch):
        data = json.loads(world.map_path.read_text())
        del data["map"][COLLEAGUE]
        world.map_path.write_text(json.dumps(data))
        world.seed()
        _set(world, "on")
        assert world.advance().status_code == 422  # control: ON refuses an unmapped colleague
        _set(world, "off")
        _arm_tripwire(monkeypatch)
        assert world.advance().status_code == 201

    @pytest.mark.parametrize("case", [
        "no_anchor", "not_pr_anchor", "unqualified_no_map_table", "closed", "wrong_phase",
        "target_not_participant", "target_is_colleague", "no_colleague", "binding_mismatch",
    ])
    def test_non_github_refusals_are_unchanged(self, world, monkeypatch, case):
        kw, to, want = {}, SEAT, 422
        if case == "no_anchor":
            kw = {"github": ""}
        elif case == "not_pr_anchor":
            kw = {"github": "project/9001"}
        elif case == "unqualified_no_map_table":
            kw = {"github": "pr/9001"}
            world.map_path.unlink()          # nothing to derive the repo from
        elif case == "closed":
            kw = {"status": "merged"}
        elif case == "wrong_phase":
            kw, want = {"phase": "fairy"}, 409
        elif case == "target_not_participant":
            to = "example-agent-stranger"
        elif case == "target_is_colleague":
            to = COLLEAGUE
        elif case == "no_colleague":
            kw = {"colleague": None}
        elif case == "binding_mismatch":
            kw = {"github": "pr/example-org/other-repo/9001"}
        world.seed(**kw)
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = world.advance(to=to)
        assert resp.status_code == want, resp.get_json()
        assert world.fields()[0].get("phase") != "merge"
        assert trip.calls == []


class TestOffFlip:
    def test_strict_flip_spelling_proceeds_like_advance(self, world, monkeypatch):
        world.seed()
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = world.flip(to=SEAT, agent=COLLEAGUE, transition_phase="merge")
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["warnings"] == [OFF_WARNING]
        assert body["ci"]["state"] == "unknown" and body["approval"]["state"] == "unknown"
        assert trip.calls == []

    def test_weak_flip_is_never_refused_for_a_github_reason(self, world, monkeypatch):
        # No colleague -> the weak gate. ON: red CI refuses; OFF: no GitHub read at all.
        world.seed(PID, phase="fairy", colleague=None, turn=AUTHOR)
        world.gh.ci = "red"
        _set(world, "on")
        assert world.flip(PID, to=SEAT, agent=AUTHOR, transition_phase="merge").status_code == 422
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = world.flip(PID, to=SEAT, agent=AUTHOR, transition_phase="merge")
        assert resp.status_code == 201, resp.get_json()
        assert trip.calls == []

    def test_flip_non_github_refusal_unchanged(self, world, monkeypatch):
        world.seed(PID, phase="fairy", colleague=None, turn=AUTHOR, github="pr/9001")
        world.map_path.unlink()
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = world.flip(PID, to=SEAT, agent=AUTHOR)
        assert resp.status_code == 422
        assert "repo-qualified" in resp.get_json()["error"]
        assert trip.calls == []

    def test_weak_flip_off_writes_the_audit_note_and_returns_the_warning(self, world, monkeypatch):
        world.seed(PID, phase="fairy", colleague=None, turn=AUTHOR)
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = world.flip(PID, to=SEAT, agent=AUTHOR, transition_phase="merge")
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["warnings"] == [OFF_WARNING]
        assert {"seq", "turn", "status"} <= set(body)
        lines = world.log_lines(FLIP_NOTE, PID)
        assert len(lines) == 1, lines
        assert trip.calls == []

    def test_weak_flip_off_without_a_pr_anchor_writes_no_note(self, world, monkeypatch):
        world.seed(PID, phase="fairy", colleague=None, turn=AUTHOR, github=None)
        _set(world, "off")
        _arm_tripwire(monkeypatch)
        resp = world.flip(PID, to=SEAT, agent=AUTHOR)
        assert resp.status_code == 201, resp.get_json()
        assert "warnings" not in resp.get_json()
        assert not world.log_lines("GitHub integration", PID)

    @pytest.mark.parametrize("mode", ["on", None])
    def test_weak_flip_on_is_unchanged(self, world, mode):
        world.seed(PID, phase="fairy", colleague=None, turn=AUTHOR)
        _set(world, mode)
        resp = world.flip(PID, to=SEAT, agent=AUTHOR, transition_phase="merge")
        assert resp.status_code == 201, resp.get_json()
        assert set(resp.get_json()) == {"seq", "turn", "status"}
        assert not world.log_lines("GitHub integration", PID)
        assert not world.log_lines("not verified", PID)


class TestOffBoardAndOtherRoutes:
    def test_board_pr_state_read_is_unknown_with_the_off_detail_and_no_gh(self, tmp_path, monkeypatch):
        trip = _arm_tripwire(monkeypatch)
        (tmp_path / "svc").mkdir()
        server_mod._configure_gate_github_identity(
            str(tmp_path / "svc"), environ={"GH_TOKEN": TOKEN}, github_integration="off")
        assert board_projects._gh_pr_state("9001", REPO) == "unknown"
        assert board_projects._GH_IDENTITY_PROVIDER() == ({}, ("unknown", OFF_DETAIL))
        assert trip.calls == []

    def test_gate_primitives_never_spawn_gh_when_off(self, tmp_path, monkeypatch):
        trip = _arm_tripwire(monkeypatch)
        (tmp_path / "svc").mkdir()
        server_mod._configure_gate_github_identity(
            str(tmp_path / "svc"), environ={"GH_TOKEN": TOKEN}, github_integration="off")
        gate = server_mod._load_baton_gate()
        for fn, args in ((gate._pr_ci_state, ("9001", REPO)),
                         (gate._reviewer_approval_state, ("9001", REPO, "example-gh-colleague")),
                         (gate._pr_approval_state, ("9001", REPO))):
            assert fn(*args)[0] == "unknown"
        assert trip.calls == []

    def test_merge_and_gc_routes_spawn_no_gh_when_off(self, world, monkeypatch):
        world.seed()
        _set(world, "off")
        server_mod._configure_gate_github_identity(
            str(world.tmp_path), environ={}, github_integration="off")
        trip = _arm_tripwire(monkeypatch)
        for route in ("merge", "gc"):
            world.client.post(f"/api/projects/{PID}/{route}",
                              json={"agent": AUTHOR, "new_status": "merged"})
        assert trip.calls == []


class TestOffSpawnsNothingEvenWithAUsableToken:
    """M4's tripwire alone only catches a spawn because the test app has no service
    identity. Here the gate HAS a token (as if the operator set =off on a box that
    still carries one): OFF itself must suppress every gh call."""

    @pytest.fixture
    def armed(self, world, monkeypatch):
        (world.tmp_path / "svc").mkdir()
        server_mod._configure_gate_github_identity(
            str(world.tmp_path / "svc"), environ={"GH_TOKEN": TOKEN})   # token-bearing identity
        ident, refusal = world.gate.gh_call_identity()
        assert refusal is None and ident["env"]["GH_TOKEN"] == TOKEN, "precondition: a usable token"
        _set(world, "off")
        return _arm_tripwire(monkeypatch)

    @staticmethod
    def _seed_anchored(world, n, **kw):
        world.seed(f"PR-{n}", github=f"pr/{REPO}/{n}", **kw)
        return f"PR-{n}"

    def test_advance_spawns_nothing(self, world, armed):
        world.seed()
        assert world.advance().status_code == 201
        assert armed.calls == []

    def test_strict_and_weak_flip_spawn_nothing(self, world, armed):
        world.seed()
        assert world.flip(to=SEAT, agent=COLLEAGUE, transition_phase="merge").status_code == 201
        weak = self._seed_anchored(world, 9101, phase="fairy", colleague=None, turn=AUTHOR)
        resp = world.flip(weak, to=SEAT, agent=AUTHOR, transition_phase="merge")
        assert resp.status_code == 201, resp.get_json()
        assert armed.calls == []

    @pytest.mark.parametrize("route", ["gc", "merge"])
    def test_gc_and_merge_spawn_nothing(self, world, armed, route):
        pid = self._seed_anchored(world, 9102)
        assert _route(world, route, AUTHOR, pid=pid).status_code == 201
        assert armed.calls == []


# ---------------------------------------------------------------------------
# C2b -- OFF: /gc and /merge on a PR-anchored baton take the UNANCHORED path
#
# OFF means advisory, never a refusal. The server cannot read the PR, so a
# parseable repo-qualified PR anchor is treated exactly like an unanchored baton
# (#3416 ruling (a)): the client's word is trusted, but the caller must be a
# participant (403 otherwise). The expected_anchor binding is unchanged. A
# NON-EMPTY anchor that does not parse still fails closed (503): that is
# malformed data, not a GitHub read.
# ---------------------------------------------------------------------------

NOT_VERIFIED_NOTE = "(GitHub integration is off; PR state not verified)"
NOT_VERIFIED_WARNING = "GitHub integration is off; PR state not verified"
OUTSIDER = "example-agent-outsider"
NEW_ANCHOR = f"pr/{REPO}/9002"


def _snap(world, pid=PID):
    return world.store.read_project(pid), world.allocator.current()


def _route(world, route, agent, pid=PID, new_status="merged"):
    body = {"agent": agent}
    if route == "gc":
        body.update({"new_status": new_status, "reason": "swept"})
    return world.client.post(f"/api/projects/{pid}/{route}", json=body)


def _stored_text(world, pid=PID):
    """The baton's text wherever it ended up (merge archives it)."""
    live = world.store.read_project(pid)
    if live is not None:
        return live
    return (world.store.root / "archive" / f"{pid}.md").read_text()


@pytest.mark.parametrize("route", ["merge", "gc"])
class TestOffPrAnchoredTakesTheUnanchoredPath:
    def test_participant_succeeds_with_note_warning_and_no_gh(self, world, monkeypatch, route):
        world.seed()
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        resp = _route(world, route, AUTHOR)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["status"] == "merged"
        assert body["warnings"] == [NOT_VERIFIED_WARNING]
        text = _stored_text(world)
        assert "status: merged" in text
        assert NOT_VERIFIED_NOTE in text
        assert trip.calls == []

    def test_non_participant_is_403_nothing_written(self, world, monkeypatch, route):
        world.seed()
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        before = _snap(world)
        resp = _route(world, route, OUTSIDER)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == f"agent {OUTSIDER!r} is not a participant of {PID}"
        assert _snap(world) == before
        assert trip.calls == []

    @pytest.mark.parametrize("bad", ["https://github.com/example-org/example-repo/pull/9001",
                                     "pr/example-org/example-repo/9001/"])
    def test_unparseable_anchor_still_503_nothing_written(self, world, monkeypatch, route, bad):
        world.seed(github=bad)
        _set(world, "off")
        trip = _arm_tripwire(monkeypatch)
        before = _snap(world)
        resp = _route(world, route, AUTHOR)
        assert resp.status_code == 503
        assert _snap(world) == before
        assert trip.calls == []

    def test_anchor_drift_between_read_and_write_is_refused(self, world, monkeypatch, route):
        """Mirrors the ON round-3 TOCTOU test: the binding is unchanged in OFF."""
        from forum import coordination as _coord
        world.seed()
        _set(world, "off")
        real = server_mod._server_pr_read
        seen = {}

        def racing(st, pid, **kw):
            out = real(st, pid, **kw)
            _coord.anchor(world.store, world.allocator, PID, github_anchor=NEW_ANCHOR)
            seen["text"], seen["seq"] = _snap(world)
            return out

        monkeypatch.setattr(server_mod, "_server_pr_read", racing)
        resp = _route(world, route, AUTHOR)
        assert resp.status_code == 409
        assert "anchor changed" in resp.get_json()["error"].lower()
        assert _snap(world) == (seen["text"], seen["seq"])
        assert "status: in-progress" in seen["text"]

    def test_unanchored_baton_in_off_gets_no_not_verified_note(self, world, monkeypatch, route):
        world.seed(pid="PR-9002", github=None)
        _set(world, "off")
        resp = _route(world, route, AUTHOR, pid="PR-9002")
        assert resp.status_code == 201
        assert "warnings" not in resp.get_json()
        assert NOT_VERIFIED_NOTE not in _stored_text(world, "PR-9002")


@pytest.mark.parametrize("route", ["merge", "gc"])
class TestOnUnknownStill503:
    def test_on_with_gh_unknown_is_503_nothing_written(self, world, monkeypatch, route):
        world.seed()
        _set(world, "on")
        monkeypatch.setattr(board_projects, "_gh_pr_state", lambda n, r: "unknown")
        before = _snap(world)
        resp = _route(world, route, AUTHOR)
        assert resp.status_code == 503
        assert _snap(world) == before


# ---------------------------------------------------------------------------
# C1 unset + no token -- LEGACY-CLOSED: exactly the pre-switch behaviour (refuses)
#
# Pre-switch behaviour was established by running the same setup (gate configured
# with no token, a PR-anchored baton) on origin/dev 38886666 (a detached worktree):
#   advance -> 422 "approval check refused ... (GitHub gate identity not configured ...)"
#   flip to the maintainer -> the same 422 (the strict flip is the advance gate)
#   /gc, /merge -> 503 "could not read the state of PR ... nothing written ..."
# Each cell asserts those, and also that mode "on" (what the resolver returns) and
# mode None (no service mode, literally the pre-switch code path) answer identically.
# ---------------------------------------------------------------------------

def _legacy_closed(world, tmp_path, mode):
    """Set the world up the way main() does for unset + no GH_TOKEN."""
    if mode == "resolved":
        mode, _why = server_mod._resolve_github_integration({})
    world.app.config["GITHUB_INTEGRATION"] = mode
    server_mod._configure_gate_github_identity(
        str(tmp_path / "svc"), environ={}, github_integration=mode)


@pytest.mark.parametrize("mode", ["resolved", None], ids=["resolved", "no-service-mode"])
class TestLegacyClosed:
    def test_advance_refuses_like_before_the_switch(self, world, tmp_path, monkeypatch, mode):
        world.seed()
        _legacy_closed(world, tmp_path, mode)
        trip = _arm_tripwire(monkeypatch)
        before = _snap(world)
        resp = world.advance()
        assert resp.status_code == 422                       # 38886666: 422
        err = resp.get_json()["error"]
        assert err.startswith("approval check refused on PR #9001")
        gate = server_mod._load_baton_gate()
        assert gate._SERVICE_IDENTITY_MISSING_MSG in err      # today's no-identity text
        assert OPT_OUT_HINT in err                            # plus the opt-out hint
        assert "GitHub integration is off" not in err         # not the advisory path
        assert world.fields()[0].get("phase") != "merge"
        assert _snap(world) == before
        assert trip.calls == []

    def test_flip_to_the_maintainer_refuses_like_before_the_switch(
            self, world, tmp_path, monkeypatch, mode):
        world.seed()
        _legacy_closed(world, tmp_path, mode)
        trip = _arm_tripwire(monkeypatch)
        before = _snap(world)
        resp = world.flip()
        assert resp.status_code == 422                       # 38886666: 422
        assert "approval check refused" in resp.get_json()["error"]
        assert world.fields()[0].get("phase") != "merge"
        assert _snap(world) == before
        assert trip.calls == []

    @pytest.mark.parametrize("route", ["merge", "gc"])
    def test_gc_and_merge_are_503_unknown_nothing_written(
            self, world, tmp_path, monkeypatch, mode, route):
        world.seed()
        _legacy_closed(world, tmp_path, mode)
        trip = _arm_tripwire(monkeypatch)
        before = _snap(world)
        resp = _route(world, route, AUTHOR)
        assert resp.status_code == 503                       # 38886666: 503
        assert "could not read the state of PR" in resp.get_json()["error"]
        assert _snap(world) == before
        assert trip.calls == []


def test_legacy_closed_answers_exactly_like_no_service_mode(world, tmp_path):
    """The resolved legacy mode and the literal pre-switch path give the same body."""
    world.seed()
    _legacy_closed(world, tmp_path, None)
    legacy_pre = world.advance().get_json()["error"]
    _legacy_closed(world, tmp_path, "resolved")
    now = world.advance().get_json()["error"]
    assert now == legacy_pre


# ---------------------------------------------------------------------------
# C3 -- ON is unchanged
# ---------------------------------------------------------------------------

class TestOnUnchanged:
    @pytest.mark.parametrize("mode", ["on", None])
    def test_green_ci_and_current_approval_advances_with_real_verdicts(self, world, mode):
        world.seed()
        _set(world, mode)
        body = world.advance().get_json()
        assert body["ci"]["state"] == "green"
        assert body["approval"]["state"] == "approved_current"
        assert body["warnings"] == []
        assert not world.log_lines("GitHub integration")

    @pytest.mark.parametrize("mode", ["on", None])
    def test_red_ci_still_refuses(self, world, mode):
        world.seed()
        world.gh.ci = "red"
        _set(world, mode)
        assert world.advance().status_code == 422

    @pytest.mark.parametrize("mode", ["on", None])
    def test_no_approval_still_refuses(self, world, mode):
        world.seed()
        world.gh.reviews = []
        _set(world, mode)
        resp = world.advance()
        assert resp.status_code == 422
        assert "approval check refused" in resp.get_json()["error"]

    @pytest.mark.parametrize("mode", ["on", None])
    def test_unknown_approval_still_refuses(self, world, mode):
        world.seed()
        world.gh.review_mode = "nonzero"
        _set(world, mode)
        assert world.advance().status_code == 422


# ---------------------------------------------------------------------------
# C4 -- installer
# ---------------------------------------------------------------------------

_TOKEN_FILE = "/etc/engram-forum/github-token.env"


class TestInstaller:
    def _lines(self, tmp_path, args):
        r, _, _ = _install(tmp_path, args)
        assert r.returncode == 0, r.stdout + r.stderr
        body = _noncomment(_rendered(r.stdout))
        return [ln for ln in body.splitlines() if "FORUM_GITHUB_INTEGRATION" in ln], body

    def test_with_token_file_renders_on(self, tmp_path):
        lines, body = self._lines(tmp_path, ["--github-token-file", _TOKEN_FILE])
        assert lines == ["Environment=FORUM_GITHUB_INTEGRATION=on"]
        assert f"EnvironmentFile=-{_TOKEN_FILE}" in body

    def test_without_token_file_renders_off(self, tmp_path):
        lines, body = self._lines(tmp_path, [])
        assert lines == ["Environment=FORUM_GITHUB_INTEGRATION=off"]
        assert "EnvironmentFile" not in body

    def test_the_line_sits_inside_service(self, tmp_path):
        r, _, _ = _install(tmp_path, [])
        text = _rendered(r.stdout)
        section = text.split("[Service]", 1)[1].split("[Install]", 1)[0]
        assert "Environment=FORUM_GITHUB_INTEGRATION=off" in section

    def _service_section(self, tmp_path, args):
        r, _, _ = _install(tmp_path, args)
        assert r.returncode == 0, r.stdout + r.stderr
        text = _rendered(r.stdout)
        return text.split("[Service]", 1)[1].split("[Install]", 1)[0]

    @pytest.mark.parametrize("args", [[], ["--github-token-file", _TOKEN_FILE]], ids=["off", "on"])
    def test_unit_does_not_restart_on_the_config_refusal_exit(self, tmp_path, args):
        section = self._service_section(tmp_path, args)
        lines = [ln.strip() for ln in section.splitlines()]
        assert lines.count("RestartPreventExitStatus=78") == 1
        # next to Restart=, not elsewhere
        assert lines.index("RestartPreventExitStatus=78") == lines.index("Restart=on-failure") + 1
        # 2 (boot-verify, possibly transient) must still restart
        assert not any(ln.startswith("RestartPreventExitStatus") and "2" in ln.split("=", 1)[1].split()
                       for ln in lines)

    def test_unit_pin_matches_the_exit_code_main_actually_uses(self, tmp_path, monkeypatch):
        t = TestMainStartup()
        t._argv(tmp_path, monkeypatch, {"FORUM_GITHUB_INTEGRATION": "on"}, False)
        with pytest.raises(SystemExit) as ei:
            server_mod.main()
        section = self._service_section(tmp_path / "inst", [])
        assert f"RestartPreventExitStatus={ei.value.code}" in section

    def _temp_src(self, tmp_path, template_text):
        """A hermetic --src that mirrors the WHOLE real src root; only the unit template differs.

        Walk from the repo root down to the template: at each level every sibling is
        symlinked and only the directory on the way down is recreated. A file the
        installer starts requiring later is then present without anyone editing a
        list here (#3574 added tools/forum_backup.py and broke a hand-picked one).
        """
        from forum.tests.test_installer import _REPO_ROOT, _TEMPLATE
        src = tmp_path / "altsrc"
        src.mkdir(parents=True)
        rel = _TEMPLATE.relative_to(_REPO_ROOT).parts
        real, alt = _REPO_ROOT, src
        for part in rel[:-1]:
            for entry in real.iterdir():
                if entry.name != part:
                    (alt / entry.name).symlink_to(entry)
            real, alt = real / part, alt / part
            alt.mkdir()
        for entry in real.iterdir():
            if entry.name != rel[-1]:
                (alt / entry.name).symlink_to(entry)
        (alt / rel[-1]).write_text(template_text)
        return src

    def _run_alt(self, tmp_path, template_text, extra=()):
        from forum.tests.test_installer import _INSTALLER, _stub_path
        src = self._temp_src(tmp_path, template_text)
        home = tmp_path / "home"
        (home / ".config" / "systemd" / "user").mkdir(parents=True)
        stub, _log = _stub_path(tmp_path, "agents")
        env = dict(os.environ, HOME=str(home), PATH=f"{stub}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run(
            ["bash", str(_INSTALLER), "--src", str(src), "--service-dir", str(tmp_path / "forum"),
             "--group", "agents", "--dry-run", *extra],
            capture_output=True, text=True, env=env)

    def test_installer_refuses_a_template_that_never_had_the_switch_placeholder(self, tmp_path):
        from forum.tests.test_installer import _TEMPLATE
        text = _TEMPLATE.read_text()
        assert "@@GITHUB_TOKEN_ENVFILE_LINE@@" in text       # control: the real one has it
        stripped = "\n".join(ln for ln in text.splitlines()
                             if ln.strip() != "@@GITHUB_TOKEN_ENVFILE_LINE@@") + "\n"
        r = self._run_alt(tmp_path / "bad", stripped)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "FORUM_GITHUB_INTEGRATION" in r.stderr
        assert "rendered unit begin" not in r.stdout          # refused before rendering out

    def test_installer_accepts_the_unmodified_template_in_the_same_harness(self, tmp_path):
        """Control for the cell above: the harness itself passes with the real template."""
        from forum.tests.test_installer import _TEMPLATE
        r = self._run_alt(tmp_path / "good", _TEMPLATE.read_text())
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Environment=FORUM_GITHUB_INTEGRATION=off" in r.stdout


# ---------------------------------------------------------------------------
# C5 -- client: no gh on PATH => warn and proceed
# ---------------------------------------------------------------------------

class TestClientFlipWithoutGh:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch, tmp_path):
        from forum.tests import test_baton_thin_client as tc
        self.tc = tc
        monkeypatch.setattr(tc._baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(tc._baton, "_POOL_SENTINEL_CACHE", [])
        empty = tmp_path / "emptybin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))   # no gh anywhere

    def _raw(self, github):
        return self.tc._make_raw_baton(
            "PR-99", github=github, turn="borges", colleague="ariadne",
            participants="[borges, ariadne, maintainer]", author="borges", phase="peer")

    def test_flip_to_the_maintainer_warns_and_proceeds(self, capsys):
        tc = self.tc
        client = tc._make_client(post_return={"seq": 7}, get_return={"raw": self._raw(f"pr/{REPO}/99")})
        args = tc._ns(project_id="PR-99", to="maintainer", reason="ready")
        tc._baton.cmd_flip(args, tc.MULTI_CONFIG, "borges", client)
        err = capsys.readouterr().err
        assert "could not verify CI state" in err
        assert "could not verify post-approval state" in err
        client.post.assert_called_once()

    def test_unqualified_anchor_refusal_unchanged(self, capsys):
        tc = self.tc
        client = tc._make_client(post_return={"seq": 7}, get_return={"raw": self._raw("pr/99")})
        args = tc._ns(project_id="PR-99", to="maintainer", reason="ready")
        with pytest.raises(SystemExit) as ei:
            tc._baton.cmd_flip(args, tc.MULTI_CONFIG, "borges", client)
        assert ei.value.code == tc.EXIT_VALIDATION
        assert "repo-qualified" in capsys.readouterr().err
        client.post.assert_not_called()
