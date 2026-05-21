"""Test that empirical-calibration constants are at the v4-study values.

These constants are the single source of truth for dedup tightness and
action-hint tier thresholds. Drift in either direction would change recall
or precision on every dedup call across the server. The calibration study
used tight-semantic-group ROC AUC analysis (0.984 AUC, peak F1 0.843 at t=0.41).

If a future change wants to move these values, that change should:
  1. Re-run the calibration study (or a new equivalent) and update the
     committed report.
  2. Update both the constant value and this test in the same commit.
"""
import server


def test_dedup_top_k_at_v4_value():
    """v4 study: dv_0446 at 0.75 sim was cut by top_k=5; 15 catches it."""
    assert server.DEDUP_TOP_K == 15


def test_dedup_min_similarity_at_v4_value():
    """v4 study: peak F1 at t=0.41; 0.40 chosen as recall-leaning operating point."""
    assert server.DEDUP_MIN_SIMILARITY == 0.40


def test_action_hint_corroborate_threshold_at_v4_value():
    """v4 study: 0.80 fired on only 9.5% of true relateds; 0.65 catches 36%."""
    assert server.ACTION_HINT_CORROBORATE_THRESHOLD == 0.65


def test_action_hint_related_threshold_at_v4_value():
    """v4 study: tier shifted from 0.60 to 0.50 to align with F1-peak region."""
    assert server.ACTION_HINT_RELATED_THRESHOLD == 0.50


def test_query_pattern_balanced_threshold_at_v4_value():
    """v4 study: balanced preset 0.65 → 0.55 (recall 14% → 27% at this tier)."""
    assert server.PATTERN_QUERY_PRESETS["balanced"]["cosine_threshold"] == 0.55


def test_action_hint_tier_ordering():
    """CORROBORATE must remain strictly above RELATED — tiers are ordered."""
    assert (
        server.ACTION_HINT_CORROBORATE_THRESHOLD
        > server.ACTION_HINT_RELATED_THRESHOLD
    )


def test_dedup_min_similarity_below_corroborate_tier():
    """Dedup floor must be below corroborate tier — else nothing reaches CORROBORATE."""
    assert server.DEDUP_MIN_SIMILARITY < server.ACTION_HINT_CORROBORATE_THRESHOLD
