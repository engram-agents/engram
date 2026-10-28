"""Structural pins for the person-node fairy spec changes (#3445, #3447).

#3445: category-10 output made the dream master re-adjudicate findings already
ruled on and waiting for a human. The fix is a per-seat ledger the master
writes and the fairy reads, with matches reported under their own heading.
#3447: P5 sampled the top-10 most-recalled observations, a biased sample that
manufactured an "under-wired" signal. The fix is a uniform seeded sample that
reports a rate with its denominator.

These are text-structure tests on two agent specs; the specs are the contract.
"""
from __future__ import annotations

import pathlib
import re
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[1]
FAIRY_MD = REPO / "src/engram/agents/claude/engram-person-node-fairy.md"
MASTER_MD = REPO / "src/engram/agents/claude/engram-dream-master.md"

LEDGER_PATH = "~/.engram/dream/person-fairy-known.json"
KNOWN_HEADING = "## Known, awaiting ruling"


def _p5_section() -> str:
    text = FAIRY_MD.read_text(encoding="utf-8")
    start = text.index("**P5")
    end = text.index("**P6", start)
    # collapse hard line wraps so phrase assertions do not depend on wrapping
    return re.sub(r"\s+", " ", text[start:end])


def test_fairy_spec_names_the_ledger_path_and_known_heading():
    text = FAIRY_MD.read_text(encoding="utf-8")
    assert LEDGER_PATH in text
    assert KNOWN_HEADING in text


def test_fairy_spec_states_the_ledger_failure_modes():
    """Missing ledger -> as today; unparseable -> loud at top; never drop."""
    text = re.sub(r"\s+", " ", FAIRY_MD.read_text(encoding="utf-8"))
    assert "unparseable" in text.lower()
    assert "never silently drop" in text.lower()


def test_fairy_spec_ledger_entry_shape_is_documented():
    text = FAIRY_MD.read_text(encoding="utf-8")
    for field in ('"version"', '"entries"', '"key"', '"status"',
                  '"reason"', '"ruling_ref"', '"since"',
                  "awaiting-ruling", "declined"):
        assert field in text, f"ledger shape field {field} missing from fairy spec"


def test_p5_no_longer_samples_by_recall_count():
    p5 = _p5_section().lower()
    assert "recall count" not in p5
    assert "recall_count" not in p5
    assert "most-recalled" not in p5
    assert "top-10" not in p5


def test_p5_samples_uniformly_with_printed_seed():
    p5 = _p5_section().lower()
    assert "uniformly at random" in p5
    assert "seed" in p5


def test_p5_reports_rate_with_denominator_and_labels_examples():
    p5 = _p5_section().lower()
    assert "denominator" in p5
    assert "illustrative examples, not a ranking" in p5


def test_p5_matches_names_on_whole_words_case_sensitive_when_short():
    p5 = _p5_section()
    assert "whole word" in p5.lower()
    assert "case-sensitive" in p5.lower()
    assert "4 characters or fewer" in p5
    assert "Annex" in p5 and "Ann-Marie" in p5  # the worked false-match examples


def test_p5_does_not_decide_the_about_edge_norm():
    p5 = _p5_section().lower()
    assert "rate" in p5 and "norm" in p5
    assert "does not imply a norm" in p5


def test_dream_master_owns_the_ledger_as_only_writer():
    text = re.sub(r"\s+", " ", MASTER_MD.read_text(encoding="utf-8"))
    assert LEDGER_PATH in text
    assert "only writer" in text.lower()
    # lifecycle: removal once the ruling is applied
    i = text.index(LEDGER_PATH)
    window = text[max(0, i - 1500): i + 2500].lower()
    assert "removes" in window or "remove" in window


def test_no_install_local_node_ids_in_either_spec():
    pat = re.compile(r"\b(ob|dv|cs|ls|fl|qu|tk|cj|ax|gl|pn|ev|df|ct)_[0-9]{4}\b")
    for path in (FAIRY_MD, MASTER_MD):
        hits = pat.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name} carries install-local node ids"


# ---- round 2 pins (review of #3465) ----------------------------------------


def _fairy_flat() -> str:
    return re.sub(r"\s+", " ", FAIRY_MD.read_text(encoding="utf-8"))


def _master_flat() -> str:
    return re.sub(r"\s+", " ", MASTER_MD.read_text(encoding="utf-8"))


def _ledger_section() -> str:
    text = FAIRY_MD.read_text(encoding="utf-8")
    start = text.index(KNOWN_HEADING)
    end = text.index("# Lottery integration", start)
    return re.sub(r"\s+", " ", text[start:end])


def test_ledger_entries_carry_count_at_ruling_and_grown_subsection():
    fairy = _fairy_flat()
    assert '"count_at_ruling"' in _ledger_section()
    assert "was M at ruling" in fairy
    assert "### Grown since ruling" in fairy
    master = _master_flat()
    assert "count_at_ruling" in master
    assert "never silently re-baseline" in master.lower()


def test_p1_ledger_key_names_a_cluster_with_variants_subset_rule():
    ledger = _ledger_section()
    assert '"variants"' in ledger
    low = ledger.lower()
    assert "subset" in low
    assert "new variant" in low
    assert "P1:<canonical person" in ledger
    assert "variants" in _master_flat()


def test_p5_is_never_ledgered():
    ledger = _ledger_section()
    assert "P5:<node_id>" not in ledger
    assert "P5 is never ledgered" in ledger
    assert "P5:<node_id>" not in _master_flat()
    assert "P5 is never ledgered" in _master_flat()


def test_no_current_match_subsection_and_master_removal():
    assert "### Ledger entries with no current match" in _fairy_flat()
    master = _master_flat()
    assert "no current match" in master
    i = master.index("no current match")
    window = master[max(0, i - 400): i + 600].lower()
    assert "remove" in window
    assert "declined" in window


def test_p5_sampling_is_executable_python_with_seed_and_population():
    p5 = _p5_section()
    assert "random.Random(seed)" in p5
    assert "population" in p5.lower()
    assert "min(400" in p5
    assert "n < 30" in p5
    assert "is_current = 1" in p5
    assert "observation_factual" in p5 and "observation_predictive" in p5
    assert "sorted" in p5.lower()
    assert "read-only" in p5.lower()


def test_p5_whole_word_rule_is_written_out_with_hyphen_and_cjk():
    p5 = _p5_section()
    assert "(?<!-)" in p5 and "(?!-)" in p5  # hyphen is excluded on both sides
    assert "Ann-Marie" in p5
    assert "aliases" in p5.lower() and "by length" in p5.lower()
    assert "CJK" in p5 and "substring" in p5.lower()


def test_key_neighbors_p5_row_carries_rate_line_and_examples():
    text = FAIRY_MD.read_text(encoding="utf-8")
    row = next(ln for ln in text.splitlines() if ln.startswith("| P5 |"))
    assert "rate line" in row
    assert "10" in row and "illustrative" in row
    assert "The observation + the target person node" not in row


def test_watermark_paragraph_p5_uses_window_since_for_sampling_window():
    flat = _fairy_flat()
    i = flat.index("Watermark:")
    para = flat[i: i + 500]
    assert "window_since" in para and "sampling window" in para


def test_p5_rate_line_is_a_measurement_not_a_finding():
    fairy = _fairy_flat()
    assert "measurement printed every run" in fairy
    assert "P5 finding exists only if" in fairy


# ---- round 3 pins (round-2 delta review of #3465) --------------------------


def test_p5_n_below_30_is_per_person():
    p5 = _p5_section()
    assert "mention a given person" in p5
    assert "that person's rate line" in p5
    assert "mention any person" not in p5


def test_p5_example_denominator_is_per_person_not_sample_size():
    p5 = _p5_section()
    assert "/ 400 sampled mentions" not in p5
    assert re.search(r"unwired \d+ / \d+ sampled mentions \(\d+\.\d%\)", p5)
    assert "not the sample size" in p5
    assert "the sample size and the population once" in p5.lower()


def test_p5_recipe_names_match_column_and_name_source():
    p5 = _p5_section()
    assert "`claim` column" in p5
    assert "metadata" in p5 and "`name`" in p5 and "`aliases`" in p5


def test_p5_states_about_edge_direction():
    p5 = _p5_section()
    assert "source is the observation" in p5
    assert "target is the person node" in p5


# ---- round 4 pins (colleague review of #3465) -------------------------------


def _bullet(flat: str, start: str, end: str) -> str:
    i = flat.index(start)
    return flat[i: flat.index(end, i) + len(end)]


def test_ledger_unparseable_is_loud_at_top_and_treated_as_no_ledger():
    ledger = _ledger_section()
    bullet = _bullet(ledger, "Ledger present but unparseable",
                     "A broken ledger never hides anything.")
    assert "say so loudly at the top of the report" in bullet
    assert "and behave as if there is no ledger." in bullet
    assert "keep using" not in bullet and "say nothing" not in bullet


def test_p1_variant_subset_rule_is_pinned_in_full():
    ledger = _ledger_section()
    assert ("only if the finding's variant set is a SUBSET of the stored "
            "`variants`") in ledger
    assert "overlaps" not in ledger


def test_p5_n_below_30_threshold_is_exactly_30():
    p5 = _p5_section()
    assert ("If fewer than 30 sampled observations mention a given person"
            in p5)
    assert "append `(too few to read; n < 30)`" in p5
    assert "fewer than 100" not in p5 and "n < 100" not in p5


def test_return_carries_growth_and_ledger_problem_first():
    flat = _fairy_flat()
    i = flat.index("Return: file path + top-5 TL;DR")
    ret = flat[i: i + 400]
    assert "starts with the number of grown entries" in ret
    assert "unparseable" in ret


def test_rekeyed_p1_entry_is_not_called_gone():
    fairy = _fairy_flat()
    assert "possibly re-keyed to <new key>" in fairy
    assert "possibly re-keyed" in _master_flat()
    sub = _bullet(_ledger_section(), "List every ledger entry",
                  "prunes from this list.")
    assert "Usually the finding is gone" in sub
    assert "still reported normally, as a fresh finding" in sub


def test_p3_key_is_the_dangling_pn_id_not_the_stamped_node():
    ledger = _ledger_section()
    master = _master_flat()
    for text in (ledger, master):
        assert "P3:<dangling pn_ id>" in text
        assert "P3:<node_id>" not in text
    assert "never the id of a stamped node" in ledger


def test_cjk_rule_is_a_concrete_range_check_with_worked_examples():
    p5 = _p5_section()
    assert r"一-鿿" in p5  # Han range written out
    assert "plain substring" in p5
    assert "Ann说好的" in p5 and "和小明说" in p5  # the worked examples


def _recipe_mention_re():
    """Execute the python block in the spec that defines mention_re."""
    text = FAIRY_MD.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    code = textwrap.dedent(next(b for b in blocks if "def mention_re" in b))
    ns: dict = {}
    exec(code, ns)
    return ns["mention_re"]


def test_cjk_recipe_behaves_as_the_spec_says():
    mention_re = _recipe_mention_re()

    def hit(name, text):
        return bool(mention_re(name).search(text))

    assert hit("Ann", "Ann说好的")      # Latin name next to CJK: CJK is a boundary
    assert hit("Ann", "和Ann说")
    assert hit("小明", "和小明说")       # CJK name: plain substring
    assert hit("小明", "小明说好的")
    assert hit("小明", "x小明y")         # CJK name: Latin neighbours do not block
    assert hit("Ann", "Ann's plan")
    assert not hit("Ann", "Annex")
    assert not hit("Ann", "Ann-Marie")
    assert not hit("Ann", "ann said")   # <= 4 chars: case-sensitive
    assert hit("Annabelle", "ANNABELLE said")  # longer: case-insensitive


# ---- round 5 pins (review of #3465, round-4 delta) -------------------------


def test_any_growth_goes_in_grown_since_ruling_no_threshold():
    ledger = _ledger_section()
    assert ("ANY growth at all (D >= 1, even a single new stamp; there is no "
            "minimum threshold) is ALSO listed in a short `### Grown since "
            "ruling` subsection") in ledger
    assert "If any Known entry grew (D >= 1), the TL;DR" in _fairy_flat()
    for bad in ("D > 5", "D > 1", "D >= 2"):
        assert bad not in _fairy_flat()


def test_rekeyed_carry_over_refreshes_variants_and_count_and_says_so():
    bullet = _bullet(_master_flat(), "**Category 10 person-node findings",
                     "Write the file whole and valid; never leave it half-edited.")
    assert ("carry the ruling over by writing the entry under the new key and "
            "removing the old one") in bullet
    assert ("The carried-over entry's `variants` and `count_at_ruling` are "
            "refreshed to the current finding's values") in bullet
    assert "and you say so in the dream record" in bullet


# ---- #3473: the master must not prune a re-keyed entry ----------------------


def _rekeyed_sentence() -> str:
    """The master's sentence about entries marked `possibly re-keyed`."""
    flat = _master_flat()
    opening = "An entry the fairy marks `possibly re-keyed to <new key>`"
    i = flat.find(opening)
    assert i != -1, (
        "dream-master spec no longer contains the re-keyed sentence "
        f"opening: {opening}")
    return flat[i: flat.index(". ", i) + 1]


def test_master_does_not_prune_rekeyed_entry_and_carries_ruling_over():
    """Meaning, not wording: a re-keyed entry is NOT an unmatched entry to
    prune (i), and the ruling moves by writing the new key and removing the
    old one, in that order (ii). Losing this only re-asks the human, but it
    is behaviour the master acts on."""
    s = _rekeyed_sentence()
    # (i) not treated as gone / unmatched / prunable
    assert "is not gone" in s
    low = s.lower()
    for bad in ("unmatched", "like any other", "prune"):
        assert bad not in low, f"re-keyed entry must not be framed as: {bad}"
    # close synonyms of "prune": whole-word start (so "dropped"/"deletes"
    # also hit, but an unrelated longer word like "dropdown" is the only
    # false-positive risk we accept; none occurs in the current sentence)
    m = re.search(r"\b(drop|delete|discard)\w*", low)
    assert m is None, (
        f"re-keyed entry must not be framed as removable: {m.group(0)}")
    # (ii) write under the new key, THEN remove the old one
    assert re.search(
        r"writing the entry under the new key.*removing the old one", s)
    assert "so the ruling is not lost" in s


# ---- #3965: P1 count rule, reader-computes-like-writer, P2 key shape ---------

P1_COUNT_RULE = (
    "For a P1 entry, `count_at_ruling` is the number of stamps whose author "
    "key is in the entry's `variants` and is NOT the canonical key, where the "
    "canonical key is the `pn_` id or, when there is none, the most-stamped key."
)
READER_LIKE_WRITER = (
    "You compute the current N for a P1 entry exactly as the writer computes "
    "`count_at_ruling` (stamps under the entry's non-canonical variants only), "
    "so growth means a new mis-keyed stamp."
)
P2_COUNT_RULE = (
    "A P2 key is `P2:<author key>`, and its `count_at_ruling` is the number "
    "of stamped nodes for that author key."
)


def test_p1_count_rule_excludes_the_canonical_key_with_fallback_in_one_sentence():
    assert P1_COUNT_RULE in _ledger_section()


def test_reader_computes_the_p1_count_exactly_as_the_writer_does():
    assert READER_LIKE_WRITER in _ledger_section()


def test_p2_key_shape_and_count_definition():
    ledger = _ledger_section()
    assert P2_COUNT_RULE in ledger
    assert ("`declined` is the expected common case, for example a cited "
            "outside author") in ledger
    assert ("A ledgered P2 is listed under `## Known, awaiting ruling` "
            "instead of being re-reported.") in ledger


def test_master_ledger_bullet_agrees_with_p1_p2_count_rules():
    master = _master_flat()
    bullet = _bullet(master, "**Category 10 person-node findings",
                     "never leave it half-edited.")
    assert '"P2:<author key>"' in bullet
    assert ("recording `count_at_ruling` as it stands now, counted by the "
            "fairy spec's rule for that key's category (for P1, only stamps "
            "under non-canonical variants)") in bullet


def test_stored_variants_include_the_canonical_key_so_are_not_the_counted_set():
    # Reviewer round 1 (#3998): this sentence had no pin; flipping
    # "includes" to "excludes" left the file green.
    assert ("The stored `variants` list includes the canonical key, so it is "
            "not the counted set by itself.") in _ledger_section()


def test_p3_count_is_stamped_nodes_pointing_at_the_dangling_id():
    # Reviewer round 1 (#3998): P3 counts stamped NODES, not stamps; the
    # restated rule had no pin.
    assert ("A P3 entry's `count_at_ruling` is the number of stamped nodes "
            "whose stamp points at the dangling `pn_` id.") in _ledger_section()
