"""#4255 slice 7: the acceptance line of the epic, as a test.

The retired multi-agent / single-agent axis must not be written anywhere in shipped source.
``git grep -niE 'multi_agent|multi-agent|single-agent|single_agent|multi agent|single agent'`` (case-insensitive: a capitalised "Multi-agent hosts" is the same word) over
``src/`` and ``tools/`` may hit only the lines on ALLOWED below.

Contract (written before the rewording; the cells below are the stated behaviours):

- A. The scan of the real tree finds no hit that is not allow-listed.  An entry is keyed on the file
  path plus the TEXT of the matched line (a distinctive substring), never on a line number, so an
  unrelated edit above the line cannot break it and a moved line keeps its excuse.
- B. Every allow-list entry still matches at least one hit line, and has a non-empty reason.  An
  excuse for text that is gone is a hole the word can come back through.
- C. The checker works in both directions on a scratch git repo (never on real source):
  (1) a new hit in a tracked file under ``src/`` or ``tools/`` is reported;
  (2) the same retired text on a line of an allow-listed PATH whose TEXT is not the allowed text is
      reported (the path alone excuses nothing);
  (3) a hit on an allowed line is accepted; an untracked file is not scanned (git grep only);
  (4) a file outside ``src/`` and ``tools/`` is not scanned.
- D. The mutants, run on the real hits: removing one entry reports its line; changing an allowed
  line's text reports it; keeping an entry whose line is gone is reported as stale.

Limits stated plainly: the scan is case-insensitive over six spellings.  A spelling it does not name
("multiagent") falls outside the scan; "multi_agent_mode" is hit only because it contains "multi_agent".
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PATTERN = r"multi_agent|multi-agent|single-agent|single_agent|multi agent|single agent"
SCANNED = ("src", "tools")

# (path, text, reason[, whole_line]) -> a hit line on that path may keep the word if the TEXT occurs in it
# (or, with whole_line=True, if the stripped line equals the text).  No line numbers, on purpose.
_RETIRE = "retirement machinery: the retired word is the point (#4255)"
ALLOWED = (
    # -- retirement machinery ------------------------------------------------------------------------
    ("src/build/engine/cli.py", '"--multi-agent",', _RETIRE + ": the accepted-and-ignored build flag", True),
    ("src/build/engine/cli.py", 'const="--multi-agent",', _RETIRE + ": the accepted-and-ignored build flag"),
    ("src/build/engine/manifest.py", "carries the retired ``multi_agent`` key", _RETIRE + ": docstring of the retired-key check"),
    ("src/build/engine/manifest.py", "carry the retired 'multi_agent' key", _RETIRE + ": the error the retired-key check raises"),
    ("src/build/engine/manifest.py", "that carries a ``multi_agent`` key", _RETIRE + ": docstring of the retired-key finder"),
    ("src/build/engine/manifest.py", '"multi_agent" in entry', _RETIRE + ": the retired-key finder"),
    ("src/engram/server.py", '_RETIRED_CONFIG_KEYS = ("multi_agent"', _RETIRE + ": the retired config keys list"),
    ("src/engram/tools/check-claude-md-drift.py", "--multi-agent", _RETIRE + ": the retired flag's help line", True),
    ("src/engram/tools/check-claude-md-drift.py", 'add_argument("--multi-agent"', _RETIRE + ": the accepted-and-ignored flag"),
    ("src/engram/tools/check-claude-md-drift.py", "if args.multi_agent:", _RETIRE + ": the flag's one-line notice"),
    ("src/engram/tools/check-claude-md-drift.py", 'print("--multi-agent is retired', _RETIRE + ": the flag's one-line notice"),
    ("src/engram/tools/install.sh", "--multi-agent|--single-agent) _retired_flag", _RETIRE + ": the accepted-and-ignored installer flags"),
    ("tools/agent-bootstrap", '--multi-agent)  if [[ "$RETIRED_NOTED"', _RETIRE + ": the accepted-and-ignored flag"),
    ("tools/test_pre_dispatch_check.py", "it no longer says single-agent", _RETIRE + ": docstring of the absence assert"),
    ("tools/test_pre_dispatch_check.py", '"single-agent" not in result.stdout', _RETIRE + ": the absence assert"),
    ("tools/claude_md_moves.json", "so a single-agent contributor is just as", "a pinned verbatim quote of the last sentence of a block moved out of CLAUDE.md; the checker matches it byte for byte"),
    # -- a real historical artifact --------------------------------------------------------------------
    ("src/engram/viz/agents.py", "viz-server-multi-agent-2026-05-04.md", "the real filename of a dated design note (active-work/); renaming it in prose would cite a file that does not exist"),
    # -- a fixture of the past -------------------------------------------------------------------------
    ("tools/test_migrate_to_plugin.sh", '{"multi_agent": false}', "fixture of a pre-retirement scatter-era config: the legacy key written on purpose"),
)


def _entry(e):
    path, text, reason = e[:3]
    return path, text, reason, (len(e) > 3 and e[3])


def scan(root=ROOT):
    """Return [(path, lineno, text)] for every tracked line under SCANNED that matches PATTERN."""
    out = subprocess.run(["git", "grep", "-niE", PATTERN, "--", *SCANNED], cwd=root, capture_output=True, text=True)
    assert out.returncode in (0, 1), out.stderr
    hits = []
    for raw in out.stdout.splitlines():
        path, lineno, text = raw.split(":", 2)
        hits.append((path, int(lineno), text))
    return hits


def _excused(path, text, allowed):
    for e in allowed:
        p, sub, _reason, whole = _entry(e)
        if p == path and ((text.strip() == sub) if whole else (sub in text)):
            return True
    return False


def violations(hits, allowed=ALLOWED):
    return [f"{p}:{n}: {t.strip()[:120]}" for (p, n, t) in hits if not _excused(p, t, allowed)]


def stale(hits, allowed=ALLOWED):
    """Entries that excuse no hit line (or carry no reason)."""
    bad = []
    for e in allowed:
        p, sub, reason, whole = _entry(e)
        if not reason.strip():
            bad.append(f"{p}: {sub!r}: no reason")
        if not any(hp == p and ((ht.strip() == sub) if whole else (sub in ht)) for (hp, _n, ht) in hits):
            bad.append(f"{p}: {sub!r}: matches no hit line")
    return bad


# ============================================================================
# A, B. the real tree
# ============================================================================
def test_a_no_unlisted_hit_in_src_and_tools():
    bad = violations(scan())
    assert not bad, "retired axis wording outside the allow-list (#4255 slice 7):\n" + "\n".join(bad)


def test_b_every_allow_list_entry_still_matches_a_line():
    bad = stale(scan())
    assert not bad, "stale allow-list entr(ies):\n" + "\n".join(bad)


def test_b_allow_list_is_keyed_on_text_never_a_line_number():
    for e in ALLOWED:
        _p, sub, _r, _w = _entry(e)
        assert not re.fullmatch(r"\d+|L\d+|:\d+", sub.strip()), e
        assert sub.strip(), e


def test_scan_root_is_a_git_checkout_with_the_scanned_roots():
    assert all((ROOT / d).is_dir() for d in SCANNED)
    assert scan(), "the scan found nothing at all: the retirement machinery alone should hit"


# ============================================================================
# C. both directions, on a scratch git repo
# ============================================================================
def _repo(tmp_path, files, track=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for rel, body in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
        if track:
            subprocess.run(["git", "add", rel], cwd=tmp_path, check=True)
    return tmp_path


ALLOW = (("src/a.py", "the allowed line", "scratch reason"),)


def test_c1_a_new_hit_in_a_tracked_file_is_reported(tmp_path):
    root = _repo(tmp_path, {"src/new.py": "x = 1\n# a multi-agent thing\n"})
    assert violations(scan(root), ALLOW) == ["src/new.py:2: # a multi-agent thing"]


@pytest.mark.parametrize("word", ["multi_agent", "multi-agent", "single-agent", "single_agent", "multi agent", "single agent"])
def test_c1_every_named_spelling_is_caught(tmp_path, word):
    root = _repo(tmp_path, {"tools/t.py": f"# {word}\n"})
    assert len(violations(scan(root), ALLOW)) == 1


def test_c2_same_text_on_an_allowed_path_but_a_different_line_is_reported(tmp_path):
    root = _repo(tmp_path, {"src/a.py": "ok = 1  # the allowed line is a multi-agent line\nbad = 2  # a different multi-agent line\n"})
    got = violations(scan(root), ALLOW)
    assert got == ["src/a.py:2: bad = 2  # a different multi-agent line"], got


def test_c3_an_allowed_line_is_accepted_wherever_it_sits(tmp_path):
    root = _repo(tmp_path, {"src/a.py": "\n\n\nx  # the allowed line, single-agent\n"})
    assert violations(scan(root), ALLOW) == []
    root2 = _repo(tmp_path / "two", {"src/a.py": "x  # the allowed line, single-agent\n"})
    assert violations(scan(root2), ALLOW) == []


def test_c3_the_excuse_is_per_path_not_global(tmp_path):
    root = _repo(tmp_path, {"src/b.py": "x  # the allowed line, single-agent\n"})
    assert len(violations(scan(root), ALLOW)) == 1


def test_c3_an_untracked_file_is_not_scanned(tmp_path):
    root = _repo(tmp_path, {"src/u.py": "# multi-agent\n"}, track=False)
    assert scan(root) == []


def test_c4_a_file_outside_the_scanned_roots_is_not_scanned(tmp_path):
    root = _repo(tmp_path, {"docs/x.md": "multi-agent\n", "tests/t.py": "# single-agent\n", "README.md": "multi_agent\n"})
    assert scan(root) == []


def test_c_whole_line_entry_excuses_only_the_bare_line(tmp_path):
    allow = (("src/c.py", "--multi-agent", "scratch", True),)
    root = _repo(tmp_path, {"src/c.py": "   --multi-agent\nprint('--multi-agent more')\n"})
    assert violations(scan(root), allow) == ["src/c.py:2: print('--multi-agent more')"]


# ============================================================================
# D. the mutants, on the real hits (only the allow-listed ones, so these cells measure the mutation and
#    not whatever else the tree still says)
# ============================================================================
def _allowed_hits():
    return [h for h in scan() if _excused(h[0], h[2], ALLOWED)]


def test_d_removing_an_entry_reports_its_line():
    hits = _allowed_hits()
    for i in range(len(ALLOWED)):
        got = violations(hits, ALLOWED[:i] + ALLOWED[i + 1:])
        assert got, f"removing {ALLOWED[i][:2]} reported nothing: the entry excuses no line"
        assert all(ALLOWED[i][0] in g for g in got)


def test_d_changing_an_allowed_lines_text_reports_it():
    """For EVERY entry: keep the path, keep the retired word on the line, change the rest of the text."""
    hits = _allowed_hits()
    for e in ALLOWED:
        path, sub, _r, whole = _entry(e)
        victims = [h for h in hits if h[0] == path and ((h[2].strip() == sub) if whole else (sub in h[2]))]
        assert victims, e[:2]
        changed = [(p, n, "the text was reworded but still says single-agent") if (p, n) == victims[0][:2] else (p, n, t)
                   for (p, n, t) in hits]
        got = violations(changed)
        assert any(g.startswith(f"{path}:{victims[0][1]}:") for g in got), (e[:2], got)


def test_d_an_entry_whose_line_is_gone_is_stale():
    hits = _allowed_hits()
    gone = ALLOWED + (("src/engram/server.py", "a line that is not there", "stale"),)
    assert stale(hits, gone) == ["src/engram/server.py: 'a line that is not there': matches no hit line"]


def test_d_a_hit_anywhere_new_is_reported():
    hits = _allowed_hits() + [("src/engram/server.py", 1, "# a new multi-agent mention")]
    got = violations(hits)
    assert got == ["src/engram/server.py:1: # a new multi-agent mention"], got


@pytest.mark.parametrize("word", ["Multi-agent", "Single-agent", "MULTI_AGENT", "Multi agent", "Single Agent"])
def test_c1_capitalised_spellings_are_caught_too(tmp_path, word):
    root = _repo(tmp_path, {"src/t.py": f"# {word} hosts\n"})
    assert len(violations(scan(root), ALLOW)) == 1


# ============================================================================
# E. the living docs (Borges, Office 19168: line 5 of the epic was only partly done; Ariadne's S7.5)
#    Same rule as src/ and tools/: a hit must sit on an allow-listed line (path + text, never a line number).
#    Scope: the top level of docs/internal, docs/*.md, docs/handbook/*.md and the READMEs / USER_GUIDE.
#    Out of scope BY DESIGN: docs/CHANGELOG.md and the subdirectories of docs/internal (archive, cycle, perf,
#    983-review, ...): dated reports and records that stay as written.
# ============================================================================
DOC_SCANNED = (
    ":(glob)docs/internal/*.md", ":(glob)docs/*.md", ":(glob)docs/handbook/*.md",
    "README.md", "README-AGENT.md", "USER_GUIDE.md", ":(exclude)docs/CHANGELOG.md",
)
DOC_ALLOWED = (
    ("docs/handbook/00-inventory.md", "no clean multi-agent gu)", "a verbatim, truncated quote in a dated (2026-06-05) snapshot table"),
    ("docs/internal/DEVELOPMENT.md", "from the 2026-05-02 multi-agent", "a dated historical aside about the 2026-05-02 migration"),
    ("docs/internal/2574-telemetry-design.md", "multi_agent", "a design record: it quotes the tiers.json entry shape as it stood when the design was written"),
    ("docs/internal/864-multi-env-harness-design.md", "single / multi-agent", "a design record: the harness matrix as it stood when #864 was designed"),
    # the block "Moved from CLAUDE.md ... Verbatim; moved, not edited" (Ariadne's ruling B): a dated verbatim record, pinned by
    # tools/claude_md_moves.json; the live rule sits in CLAUDE.md. One entry per quoted line that carries the words.
    ("docs/internal/PR-MERGE-POLICY.md", "it holds identically in single-agent and", "verbatim move record (2026-10-03, #3322): the quoted rule text"),
    ("docs/internal/PR-MERGE-POLICY.md", "multi-agent mode. (Multi-agent: it gates the", "verbatim move record (2026-10-03, #3322): the quoted rule text"),
    ("docs/internal/PR-MERGE-POLICY.md", "Single-agent: it gates surfacing the PR as ready", "verbatim move record (2026-10-03, #3322): the quoted rule text"),
    ("docs/internal/PR-MERGE-POLICY.md", "maintainer. Same rule, different surface", "verbatim move record (2026-10-03, #3322): the quoted rule text, also the pin's last sentence"),
)


def scan_docs(root=ROOT):
    out = subprocess.run(["git", "grep", "-niE", PATTERN, "--", *DOC_SCANNED], cwd=root, capture_output=True, text=True)
    assert out.returncode in (0, 1), out.stderr
    hits = []
    for raw in out.stdout.splitlines():
        path, lineno, text = raw.split(":", 2)
        hits.append((path, int(lineno), text))
    return hits


def test_e_no_unlisted_hit_in_the_living_docs():
    bad = violations(scan_docs(), DOC_ALLOWED)
    assert not bad, "retired axis wording in a living doc, outside the allow-list (#4255 slice 7):\n" + "\n".join(bad)


def test_e_every_docs_allow_list_entry_still_matches_a_line():
    bad = stale(scan_docs(), DOC_ALLOWED)
    assert not bad, "stale docs allow-list entr(ies):\n" + "\n".join(bad)


def test_e_a_new_hit_in_a_living_doc_is_reported(tmp_path):
    root = _repo(tmp_path, {"docs/internal/NEW.md": "# notes on multi-agent mode\n"})
    assert len(violations(scan_docs(root), DOC_ALLOWED)) == 1


def test_e_the_design_and_report_subdirectories_and_the_changelog_are_not_scanned(tmp_path):
    root = _repo(tmp_path, {
        "docs/internal/cycle/old.md": "# multi-agent\n",
        "docs/internal/archive/old.sh": "# multi-agent\n",
        "docs/CHANGELOG.md": "multi-agent history\n",
    })
    assert scan_docs(root) == []


def test_e_the_moved_policy_block_says_its_topology_wording_was_retired_and_where_the_live_rule_is():
    text = (ROOT / "docs/internal/PR-MERGE-POLICY.md").read_text(encoding="utf-8")
    block = text[text.index("## Moved from CLAUDE.md"):]
    head, _rest = block.split("*Verbatim; moved, not edited.", 1)
    after = _rest.split("\n", 1)[1]
    note = after.strip().splitlines()[0]
    assert "2026-10-29" in note and "#4255" in note and "CLAUDE.md" in note and "retired" in note, note
    assert not re.search(PATTERN, note, re.I), note


def test_e_development_md_describes_the_manifest_entries_by_the_keys_they_still_carry():
    import json
    data = json.loads((ROOT / "src/build/packaging/tiers.json").read_text(encoding="utf-8"))
    keys = {k for bucket in data.values() if isinstance(bucket, list) for e in bucket if isinstance(e, dict) for k in e}
    assert "multi_agent" not in keys, "tiers.json entries still carry the retired key"
    line = next(ln for ln in (ROOT / "docs/internal/DEVELOPMENT.md").read_text(encoding="utf-8").splitlines() if ln.startswith("- **`mechanisms[]`**"))
    assert line.endswith("shippable artifacts with tier assignments."), line


def test_the_module_docstring_does_not_contradict_the_case_insensitive_scan():
    doc = (__doc__ or "").replace("case-insensitive", "")
    assert "case-sensitive" not in doc and "is not caught here" not in doc
