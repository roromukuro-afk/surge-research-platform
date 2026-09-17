"""Whether a free tier can carry this analysis, measured rather than assumed.

The answer matters before any account is opened, and it is knowable now: the
size of the input is a property of this repository. What the tests here protect
is the shape of the conclusion - per model family, with the floor computed from
an empty bundle so that "it does not fit" is safe and "it fits" is provisional.
"""

from __future__ import annotations

from surge.analysis.groq_provider import ANALYSIS_FREE_QUOTA_BLOCKED, PUBLISHED_FREE_LIMITS
from surge.jobs.analysis_feasibility import Measurement, assess, assess_tier, measure


def test_the_canonical_prompt_is_actually_measured():
    """Not a placeholder: v5.1 is registered, and its size is a fact."""

    measurement = measure()

    assert measurement.canonical_bytes > 30_000
    assert measurement.canonical_only > 5_000
    # The floor includes the canonical prompt, the addenda and the contract, so
    # it is strictly larger than the canonical prompt alone.
    assert measurement.minimum_stage3_request > measurement.canonical_plus_addenda
    assert measurement.minimum_entry_request > measurement.canonical_plus_addenda


def test_the_floor_is_an_empty_bundle_so_a_real_request_is_larger():
    measurement = measure()

    assert measurement.minimum_entry_request >= measurement.minimum_stage3_request


def test_a_family_whose_per_minute_limit_is_below_the_floor_cannot_send_one_request():
    measurement = Measurement(
        canonical_only=9_000,
        canonical_plus_addenda=12_000,
        minimum_stage3_request=13_000,
        minimum_entry_request=13_500,
        canonical_bytes=32_000,
        addenda_files=4,
    )

    verdict = assess_tier(measurement, "tiny", {"tokens_per_minute": 8_000})

    assert not verdict["fits"]
    assert verdict["reason_code"] == ANALYSIS_FREE_QUOTA_BLOCKED
    assert "whatever the pacing" in verdict["why"]


def test_the_verdict_is_per_family_rather_than_per_provider():
    """One tier's limit is not the provider's. Collapsing them is the error that
    made the first Alpaca conclusion wrong."""

    verdict = assess(measure())

    families = {tier["model_family"] for tier in verdict["tiers"]}
    assert families == set(PUBLISHED_FREE_LIMITS)
    assert len({tier["fits"] for tier in verdict["tiers"]}) == 2


def test_an_unverified_structured_output_claim_is_not_counted_as_usable():
    """A family that fits on size but is not documented for strict mode is a
    candidate to measure, not a solution to rely on."""

    verdict = assess(measure())

    assert verdict["fits_somewhere"]
    assert not verdict["fits_with_strict_structured_outputs"]
    assert "unverified" in verdict["conclusion"]


def test_shortening_the_canonical_prompt_is_never_the_answer():
    verdict = assess(measure())

    assert "not shortened to fit a quota" in verdict["never"]
