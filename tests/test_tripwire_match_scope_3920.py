"""Contract tests for alpha #3920 (part of #3911): per-lesson `match_scope`.

engram-lesson-tripwire-hook.py matched every lesson's situation_pattern against the RAW target:
the whole Bash command (heredoc bodies included) or "{tool} {compact JSON of all args}" (prose
fields included). A pattern written for a command then fires on prose that only MENTIONS the
command. The design (Borges, #3920) gives each call two views, computed once per fire:

  full     today's target, byte for byte
  command  Bash: each heredoc BODY replaced by `<<HEREDOC>>`; the operator and everything after the
           terminator line stay. MCP: tool name + the JSON with the VALUES of prose fields replaced
           by "<<TEXT>>"; key names and non-prose values kept.

and a lesson's metadata `match_scope` (`full` default, `command` opt-in) picks the view its pattern runs on.

Contract (each cell is named after the line of the issue it pins):
  S1  quoted / unquoted / tab-stripping heredocs: body replaced, operator kept
  S2  two heredocs in one command: both bodies replaced
  S3  text after the terminator line is still visible to a `command` pattern
  S4  an UNTERMINATED heredoc falls back to the full text (a missed strip is noise, a wrong strip is blindness)
  S5  MCP: prose-field values replaced; keys, non-prose values and the tool name untouched
  S6  a `match_scope: full` lesson sees exactly today's target
  S7  discriminating control: the slice lesson stops firing on a heredoc that only quotes `sed -n`
      and still fires on a real `sed -n 1,20p file`
  S8  a lesson with no match_scope runs on the full view (the default; command is opt-in)
  S9  `full` view is byte-identical to build_match_target for Bash and MCP
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_3920", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def _mcp(tool, **args):
    return {"tool_name": tool, "tool_input": args}


# ---------------------------------------------------------------- S1 / S2 / S3 / S4
class TestHeredocStripping:
    def test_s1_quoted_heredoc_body_replaced_operator_kept(self, hook):
        cmd = "cat <<'EOF'\nsed -n 1,5p file\nEOF"
        out = hook.strip_heredoc_bodies(cmd)
        assert "<<'EOF'" in out
        assert "sed -n" not in out
        assert "<<HEREDOC>>" in out

    def test_s1_unquoted_heredoc(self, hook):
        out = hook.strip_heredoc_bodies("cat <<EOF\nsed -n 1,5p file\nEOF")
        assert "<<EOF" in out and "sed -n" not in out and "<<HEREDOC>>" in out

    def test_s1_tab_stripping_heredoc(self, hook):
        out = hook.strip_heredoc_bodies("cat <<-EOF\n\tsed -n 1,5p file\n\tEOF")
        assert "<<-EOF" in out and "sed -n" not in out and "<<HEREDOC>>" in out

    def test_s1_double_quoted_delimiter(self, hook):
        out = hook.strip_heredoc_bodies('cat <<"END"\nrm -rf x\nEND')
        assert "rm -rf" not in out and "<<HEREDOC>>" in out

    def test_s2_two_heredocs_both_bodies_replaced(self, hook):
        cmd = "cat <<A\nfirst body git push\nA\ncat <<'B'\nsecond body gh pr merge\nB"
        out = hook.strip_heredoc_bodies(cmd)
        assert "git push" not in out and "gh pr merge" not in out
        assert out.count("<<HEREDOC>>") == 2

    def test_s3_text_after_terminator_stays_visible(self, hook):
        cmd = "cat > /tmp/x <<'EOF'\nprose that mentions sed -n\nEOF\ngh pr create --body-file \"$B\""
        out = hook.strip_heredoc_bodies(cmd)
        assert 'gh pr create --body-file "$B"' in out
        assert "prose that mentions" not in out

    def test_s3_text_before_the_operator_stays_visible(self, hook):
        out = hook.strip_heredoc_bodies("git commit -F - <<'EOF'\nmessage\nEOF")
        assert out.startswith("git commit -F - <<'EOF'")

    def test_s4_unterminated_heredoc_falls_back_to_full_text(self, hook):
        cmd = "cat <<'EOF'\nsed -n 1,5p file\nno terminator here"
        assert hook.strip_heredoc_bodies(cmd) == cmd

    def test_s4_one_unterminated_of_two_still_returns_full_text(self, hook):
        cmd = "cat <<A\nbody\nA\ncat <<B\nnever closed"
        assert hook.strip_heredoc_bodies(cmd) == cmd

    def test_no_heredoc_is_unchanged(self, hook):
        cmd = "sed -n 1,20p file | head"
        assert hook.strip_heredoc_bodies(cmd) == cmd

    def test_here_string_is_not_a_heredoc(self, hook):
        cmd = "grep x <<< 'sed -n 1,5p'"
        assert hook.strip_heredoc_bodies(cmd) == cmd


# ---------------------------------------------------------------- S5
class TestMcpCommandView:
    def test_s5_prose_value_replaced_key_and_other_values_kept(self, hook):
        full, command = hook.build_match_views(
            _mcp("mcp__x__send_crossing", destination="place_post_sol", body="ob_1234 mentioned here"))
        assert "ob_1234" in full
        assert "ob_1234" not in command
        assert "place_post_sol" in command           # non-prose value kept
        assert '"body"' in command                    # key kept
        assert "<<TEXT>>" in command
        assert command.startswith("mcp__x__send_crossing ")   # tool name untouched

    @pytest.mark.parametrize("field", sorted(["body", "claim", "quoted_text", "interpretation", "logical_chain", "recall_summary", "description", "text", "message", "content"]))
    def test_s5_every_listed_prose_field_is_replaced(self, hook, field):
        assert field in hook.PROSE_FIELDS
        _, command = hook.build_match_views(_mcp("mcp__x__t", **{field: "SECRET-WORD"}))
        assert "SECRET-WORD" not in command

    def test_s5_prose_fields_is_one_constant(self, hook):
        assert isinstance(hook.PROSE_FIELDS, (frozenset, tuple, set))

    def test_s5_nested_args_are_walked(self, hook):
        _, command = hook.build_match_views(
            _mcp("mcp__x__batch", items=[{"claim": "hidden text", "id": "ob_9"}]))
        assert "hidden text" not in command
        assert "ob_9" in command


# ---------------------------------------------------------------- review round 1 (PR 3937)
class TestExecutedHeredocsAreNotHidden:
    @pytest.mark.parametrize("opener", ["bash <<EOF", "sh <<'EOF'", "python3 - <<'EOF'", "ssh host <<EOF",
                                        "sudo bash <<EOF", "node <<'EOF'", "cd x && bash <<EOF",
                                        "/bin/bash <<EOF", "/usr/bin/python3 - <<'PY'", "exec bash <<EOF",
                                        "env X=1 bash <<EOF", "docker exec -i c sh <<EOF",
                                        "sqlite3 knowledge.db <<SQL", "psql -d x <<EOF", "ipython <<EOF"])
    def test_body_fed_to_an_interpreter_stays_visible(self, hook, opener):
        # Terminate with the opener's OWN delimiter: an unterminated heredoc falls back to the
        # full text and would make this cell pass whatever the exec-word list says (ls_0042).
        delim = hook._heredoc_ops_on_line(opener)[0][1]
        cmd = opener + "\nrm -rf /x\n" + delim
        assert "rm -rf /x" in hook.strip_heredoc_bodies(cmd)
        # the same body fed to `cat` IS stripped, so the cell can tell the two apart
        assert "rm -rf /x" not in hook.strip_heredoc_bodies("cat <<" + delim + "\nrm -rf /x\n" + delim)

    @pytest.mark.parametrize("opener", ["cat <<EOF | bash", "cat <<EOF | sh -s", "cat <<EOF | sudo python3 -"])
    def test_body_piped_into_an_interpreter_stays_visible(self, hook, opener):
        assert "rm -rf /x" in hook.strip_heredoc_bodies(opener + "\nrm -rf /x\nEOF")

    def test_second_heredoc_on_a_line_gets_its_own_consumer(self, hook):
        out = hook.strip_heredoc_bodies("cat <<A; bash <<B\nprose\nA\nrm -rf /x\nB")
        assert "prose" not in out
        assert "rm -rf /x" in out

    def test_limit_a_heredoc_inside_double_quotes_is_not_stripped(self, hook):
        """Known limit: `git commit -m "$(cat <<'EOF' ...)"` keeps its body (the quote tracker
        skips the operator). It fails toward more matching, never toward blindness."""
        cmd = 'git commit -m "$(cat <<\'EOF\'\nsed -n prose\nEOF\n)"'
        assert "sed -n prose" in hook.strip_heredoc_bodies(cmd)

    def test_control_body_fed_to_cat_is_still_hidden(self, hook):
        assert "rm -rf /x" not in hook.strip_heredoc_bodies("cat <<EOF\nrm -rf /x\nEOF")

    def test_control_a_word_that_merely_contains_an_interpreter_name_is_not_one(self, hook):
        assert "rm -rf /x" not in hook.strip_heredoc_bodies("cat notes-bash.txt <<EOF\nrm -rf /x\nEOF")


class TestScopeSetsAgree:
    def test_hook_and_epistemic_define_the_same_scopes_and_the_default_is_one_of_them(self, hook):
        import importlib
        epi = importlib.import_module("engram_epistemic")
        assert set(hook.MATCH_SCOPES) == set(epi.MATCH_SCOPES) == {"command", "full"}
        assert hook.DEFAULT_MATCH_SCOPE in hook.MATCH_SCOPES


class TestPayloadJsonIsRedacted:
    def test_prose_inside_a_payload_json_string_is_hidden(self, hook):
        import json as _json
        args = {"payload_json": _json.dumps({"claim": "SECRET sed -n", "node_id": "ob_9"})}
        full, command = hook.build_match_views(_mcp("mcp__e__engram_add_observation", **args))
        assert "SECRET" in full
        assert "SECRET" not in command
        assert "ob_9" in command

    def test_payload_json_that_is_not_json_is_left_alone(self, hook):
        _, command = hook.build_match_views(_mcp("mcp__e__t", payload_json="not json {"))
        assert "not json {" in command


# ---------------------------------------------------------------- S9
class TestFullViewIsToday:
    def test_s9_bash_full_equals_build_match_target(self, hook):
        for cmd in ("ls", "cat <<'EOF'\nx\nEOF", "echo 'a' && git push"):
            full, _ = hook.build_match_views(_bash(cmd))
            assert full == hook.build_match_target(_bash(cmd))

    def test_s9_mcp_full_equals_build_match_target(self, hook):
        hi = _mcp("mcp__x__t", body="b", n=3)
        full, _ = hook.build_match_views(hi)
        assert full == hook.build_match_target(hi)

    def test_other_tools_give_empty_views(self, hook):
        assert hook.build_match_views({"tool_name": "Read", "tool_input": {"file_path": "/x"}}) == ("", "")


# ---------------------------------------------------------------- S6 / S7 / S8
SLICE = r"\bsed\s+-n\b"


class TestPerLessonScope:
    def _hits(self, hook, cmd, tripwires):
        full, command = hook.build_match_views(_bash(cmd))
        return hook.check_command(full, tripwires, command_target=command)

    def test_s8_no_scope_defaults_to_full_view(self, hook):
        tw = [(SLICE, "slice lesson", "ls_slice", "normal")]
        hits = self._hits(hook, "cat <<'EOF'\nsed -n 1,5p f\nEOF", tw)
        assert [h["node_id"] for h in hits] == ["ls_slice"]

    def test_s8_explicit_command_scope(self, hook):
        tw = [(SLICE, "slice lesson", "ls_slice", "normal", "command")]
        assert self._hits(hook, "cat <<'EOF'\nsed -n 1,5p f\nEOF", tw) == []

    def test_s6_full_scope_sees_the_whole_target(self, hook):
        tw = [(SLICE, "slice lesson", "ls_slice", "normal", "full")]
        hits = self._hits(hook, "cat <<'EOF'\nsed -n 1,5p f\nEOF", tw)
        assert [h["node_id"] for h in hits] == ["ls_slice"]

    def test_s7_control_stops_on_quote_still_fires_on_real_command(self, hook):
        tw = [(SLICE, "slice lesson", "ls_slice", "normal", "command")]
        assert self._hits(hook, "cat <<'EOF'\nuse sed -n to slice\nEOF", tw) == []
        real = self._hits(hook, "sed -n 1,20p file", tw)
        assert [h["node_id"] for h in real] == ["ls_slice"]

    def test_s7_real_command_after_a_heredoc_still_fires(self, hook):
        tw = [(SLICE, "slice lesson", "ls_slice", "normal")]
        cmd = "cat <<'EOF'\nprose\nEOF\nsed -n 1,20p file"
        assert [h["node_id"] for h in self._hits(hook, cmd, tw)] == ["ls_slice"]

    def test_s4_unterminated_heredoc_still_fires_command_lesson(self, hook):
        tw = [(SLICE, "slice lesson", "ls_slice", "normal")]
        assert self._hits(hook, "cat <<'EOF'\nsed -n 1,5p f\nnever closed", tw) != []

    def test_callers_that_pass_only_the_full_target_keep_todays_behaviour(self, hook):
        """check_command(match_target, tripwires) with no command_target: every lesson sees the
        full target (existing callers and tests are unchanged)."""
        tw = [(SLICE, "slice lesson", "ls_slice", "normal")]
        full, _ = hook.build_match_views(_bash("cat <<'EOF'\nsed -n 1,5p f\nEOF"))
        assert [h["node_id"] for h in hook.check_command(full, tw)] == ["ls_slice"]

    def test_hit_shape_is_unchanged(self, hook):
        tw = [(SLICE, "slice lesson", "ls_slice", "high", "full")]
        hit = self._hits(hook, "sed -n 1p f", tw)[0]
        assert set(hit) == {"nudge", "node_id", "pattern", "priority"}
        assert hit["priority"] == "high"


# ---------------------------------------------------------------- loader
def _db(tmp_path, rows):
    p = tmp_path / "knowledge.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE nodes (id TEXT, type TEXT, claim TEXT, metadata TEXT, is_current INT, memory_status TEXT)")
    for nid, meta in rows:
        c.execute("INSERT INTO nodes VALUES (?, 'lesson', 'claim text', ?, 1, 'active')", (nid, json.dumps(meta)))
    c.commit()
    c.close()
    return p


class TestLoaderReadsMatchScope:
    def test_scope_is_the_fifth_element_and_defaults_to_full(self, hook, tmp_path):
        p = _db(tmp_path, [
            ("ls_a", {"situation_pattern": "aaa", "scaffolding_nudge": "n"}),
            ("ls_b", {"situation_pattern": "bbb", "scaffolding_nudge": "n", "match_scope": "full"}),
            ("ls_c", {"situation_pattern": "ccc", "scaffolding_nudge": "n", "match_scope": "command"}),
        ])
        got = {t[2]: t for t in hook.load_tripwires(p)}
        assert got["ls_a"][4] == "full"
        assert got["ls_b"][4] == "full"
        assert got["ls_c"][4] == "command"

    def test_unknown_scope_value_falls_back_to_full_not_blind(self, hook, tmp_path):
        """A typo in match_scope must not silently blind a lesson: anything but the two known
        values is treated as `full`, today's behaviour."""
        p = _db(tmp_path, [("ls_t", {"situation_pattern": "ttt", "scaffolding_nudge": "n", "match_scope": "cmd"})])
        assert hook.load_tripwires(p)[0][4] == "full"

    def test_first_four_elements_unchanged(self, hook, tmp_path):
        p = _db(tmp_path, [("ls_a", {"situation_pattern": "aaa", "scaffolding_nudge": "nud", "tripwire_priority": "high"})])
        pattern, nudge, node_id, priority = hook.load_tripwires(p)[0][:4]
        assert (pattern, nudge, node_id, priority) == ("aaa", "nud", "ls_a", "high")
