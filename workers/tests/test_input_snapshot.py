"""The input an analysis was run against, and proving a resumed one matches.

The claim these make true: "a crash mid-analysis leaves everything needed to
finish it". Before this, the record said an analysis had started and the bundle
lived in the caller's memory, so recovery took the inputs back as arguments -
and depended on the process that died still being alive.

A recovery pass that rebuilt the bundle instead would be building a different
one: twenty minutes on, an intraday bundle is a different price, a different
tape and possibly a different universe verdict. So the test that matters is not
"can it rebuild" but "does it refuse when the rebuild differs".
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from surge.analysis.bundle import sha256_text  # noqa: E402
from surge.analysis.entry_analysis import EntryGuardFacts, IntradayBundle
from surge.analysis.execution import StoredInput
from surge.analysis.input_snapshot import (
    InputInconsistencyError,
    InputReconstructionError,
    build_stored_input,
    rebuild_bundle,
    rebuild_facts,
    reconstruct,
)
from surge.entry.models import EntryError, ObservedPrice, UniverseVerdict

CUTOFF = datetime(2026, 9, 17, 2, 15, tzinfo=UTC)
CANONICAL_TEXT = "the canonical method, in full"
CANONICAL = sha256_text(CANONICAL_TEXT)
ADDENDA = ("a later decision",)


def _bundle(**overrides) -> IntradayBundle:
    base = {
        "security_id": "sec-1",
        "market_code": "JP",
        "session_date": date(2026, 9, 17),
        "decision_cutoff_at": CUTOFF,
        "canonical_prompt_sha256": CANONICAL,
        "addenda_sha256": [sha256_text(text) for text in ADDENDA],
        "sections": {
            "live_price": {"price": "1000"},
            "stage3_setup": {"state": "WATCH_BREAKOUT"},
            "coverage": {"materials": 1.0},
        },
        "versions": {"feature_version": "1.0.0"},
    }
    base.update(overrides)
    return IntradayBundle(**base)


def _facts(**overrides) -> EntryGuardFacts:
    base = {
        "universe": UniverseVerdict(decision="INCLUDED", reason_code="TARGET_MARKET"),
        "decision_price": ObservedPrice(
            amount=Decimal("1000"), currency="JPY", observed_at=CUTOFF
        ),
        "decision_cutoff_at": CUTOFF,
        "coverage_meets_requirements": True,
        "coverage_detail": "all collectors reported",
    }
    base.update(overrides)
    return EntryGuardFacts(**base)


def _built(**kwargs):
    return build_stored_input(
        bundle=_bundle(),
        facts=_facts(),
        canonical_text=CANONICAL_TEXT,
        addenda_texts=ADDENDA,
        thesis_key="WATCH_BREAKOUT|R_A",
        **kwargs,
    )


# ------------------------------------------------- what is written down


def test_the_prompt_is_hashed_before_it_is_sent():
    """The hash is of the prompt that goes out, not of one rendered later. Both
    are returned together so a caller cannot render a second one to send -
    rendering twice would usually produce identical text, and "usually" is the
    failure."""

    stored, prompt = _built()

    assert len(stored.prompt_sha256) == 64
    assert CANONICAL_TEXT in prompt
    assert ADDENDA[0] in prompt


def test_everything_the_hard_filter_needs_is_recorded():
    """A resumed analysis re-runs the 3,000 yen rule. Without the price, the
    rate and the limit it would be re-running it on different numbers."""

    stored, _ = _built()

    assert stored.decision_price == Decimal("1000")
    assert stored.decision_price_currency == "JPY"
    assert stored.decision_price_observed_at == CUTOFF
    assert stored.price_limit_jpy == Decimal("3000")
    assert stored.universe_decision == "INCLUDED"
    assert stored.coverage_meets_requirements is True


def test_an_input_without_its_bundle_is_refused():
    """The record exists to make a crashed analysis resumable. One without the
    bundle is a note that an analysis began."""

    with pytest.raises(EntryError, match="stored bundle"):
        StoredInput(
            bundle_serialized="",
            prompt_sha256="p" * 64,
            bundle_sha256="b" * 64,
            canonical_prompt_sha256=CANONICAL,
        )


def test_a_half_length_hash_is_refused():
    with pytest.raises(EntryError, match="full SHA-256"):
        StoredInput(
            bundle_serialized="{}",
            prompt_sha256="p" * 32,
            bundle_sha256="b" * 64,
            canonical_prompt_sha256=CANONICAL,
        )


# ------------------------------------------------------- rebuilding it


def test_the_bundle_rebuilds_into_its_own_hash():
    stored, _ = _built()

    rebuilt = rebuild_bundle(stored)

    assert rebuilt.bundle_sha256 == stored.bundle_sha256
    assert rebuilt.security_id == "sec-1"
    assert rebuilt.session_date == date(2026, 9, 17)
    assert rebuilt.decision_cutoff_at == CUTOFF
    assert set(rebuilt.sections) == {"live_price", "stage3_setup", "coverage"}
    assert rebuilt.versions == {"feature_version": "1.0.0"}


def test_the_facts_come_back_as_they_were_not_as_they_are_now():
    """The universe verdict and the coverage assessment are statements about a
    moment. Re-measuring them at recovery would answer a different question."""

    stored, _ = _built()

    facts = rebuild_facts(stored)

    assert facts.universe.decision == "INCLUDED"
    assert facts.universe.reason_code == "TARGET_MARKET"
    assert facts.decision_price.amount == Decimal("1000")
    assert facts.decision_cutoff_at == CUTOFF
    assert facts.coverage_meets_requirements is True
    assert facts.coverage_detail == "all collectors reported"


def test_a_round_trip_reproduces_the_prompt_exactly():
    stored, prompt = _built()

    rebuilt = reconstruct(stored, canonical_text=CANONICAL_TEXT, addenda_texts=ADDENDA)

    assert rebuilt.prompt == prompt
    assert rebuilt.thesis_key == "WATCH_BREAKOUT|R_A"


# ------------------------------------------- and refusing when it differs


def test_a_canonical_file_that_changed_fails_the_reconstruction():
    """The strongest case for checking rather than trusting. The bundle is
    intact, the hashes on it match, and the method itself has moved - so a
    resumed analysis would be answering a different question under the original
    trigger's name."""

    stored, _ = _built()

    with pytest.raises(InputReconstructionError, match="canonical text has changed"):
        reconstruct(
            stored,
            canonical_text=CANONICAL_TEXT + " with a paragraph added since",
            addenda_texts=ADDENDA,
        )


def test_an_addendum_published_since_fails_the_reconstruction():
    stored, _ = _built()

    with pytest.raises(InputReconstructionError):
        reconstruct(
            stored,
            canonical_text=CANONICAL_TEXT,
            addenda_texts=(*ADDENDA, "a decision taken after this analysis started"),
        )


def test_a_bundle_edited_after_the_fact_fails_the_reconstruction():
    """Not a scenario the code can produce - the row is immutable in the
    database - which is exactly why it is worth asserting here: the check has to
    hold whatever put the row in that state."""

    stored, _ = _built()
    tampered = replace(
        stored,
        bundle_serialized=stored.bundle_serialized.replace('"1000"', '"900"'),
    )

    with pytest.raises(InputReconstructionError):
        reconstruct(tampered, canonical_text=CANONICAL_TEXT, addenda_texts=ADDENDA)


def test_reordered_addenda_fail_the_reconstruction():
    """Order is meaning here: a newer addendum overrides an older one."""

    stored, _ = build_stored_input(
        bundle=_bundle(addenda_sha256=[sha256_text("first"), sha256_text("second")]),
        facts=_facts(),
        canonical_text=CANONICAL_TEXT,
        addenda_texts=("first", "second"),
    )

    with pytest.raises(InputReconstructionError):
        reconstruct(stored, canonical_text=CANONICAL_TEXT, addenda_texts=("second", "first"))


# ------------------ the bundle's claims, checked before anything is sent (I)
#
# The prompt hash cannot catch these on a fresh run: it is computed from the
# same caller-supplied bundle, so it trivially matches itself. Only comparing
# each declaration with the thing it declares does.


def test_a_canonical_hash_that_is_not_the_texts_hash_is_refused():
    """The bundle would record one method as sent while another one was."""

    with pytest.raises(InputInconsistencyError, match="declares canonical"):
        build_stored_input(
            bundle=_bundle(canonical_prompt_sha256="c" * 64),
            facts=_facts(),
            canonical_text=CANONICAL_TEXT,
            addenda_texts=ADDENDA,
        )


def test_addenda_hashes_that_are_not_the_texts_hashes_are_refused():
    with pytest.raises(InputInconsistencyError, match="declares addenda"):
        build_stored_input(
            bundle=_bundle(addenda_sha256=["a" * 64]),
            facts=_facts(),
            canonical_text=CANONICAL_TEXT,
            addenda_texts=ADDENDA,
        )


def test_addenda_in_a_different_order_are_refused():
    """Order is meaning: the newer addendum overrides the older, and the prompt
    renders them in the order given."""

    with pytest.raises(InputInconsistencyError, match="declares addenda"):
        build_stored_input(
            bundle=_bundle(addenda_sha256=[sha256_text("second"), sha256_text("first")]),
            facts=_facts(),
            canonical_text=CANONICAL_TEXT,
            addenda_texts=("first", "second"),
        )


def test_a_bundle_for_a_different_cutoff_than_the_facts_is_refused():
    later = datetime(2026, 9, 17, 2, 45, tzinfo=UTC)

    with pytest.raises(InputInconsistencyError, match="assembled for cutoff"):
        build_stored_input(
            bundle=_bundle(),
            facts=_facts(
                decision_cutoff_at=later,
                decision_price=ObservedPrice(
                    amount=Decimal("1000"), currency="JPY", observed_at=later
                ),
            ),
            canonical_text=CANONICAL_TEXT,
            addenda_texts=ADDENDA,
        )


def test_a_decision_price_from_after_the_cutoff_is_refused():
    """The model would be shown a price from after the moment it is deciding
    at - leakage in its most direct form."""

    with pytest.raises(InputInconsistencyError, match="after the cutoff"):
        build_stored_input(
            bundle=_bundle(),
            facts=_facts(
                decision_price=ObservedPrice(
                    amount=Decimal("1000"),
                    currency="JPY",
                    observed_at=datetime(2026, 9, 17, 2, 16, tzinfo=UTC),
                )
            ),
            canonical_text=CANONICAL_TEXT,
            addenda_texts=ADDENDA,
        )


def test_every_problem_is_reported_not_just_the_first():
    """Fixing one at a time, one run at a time, is how a half-understood input
    ends up being sent."""

    with pytest.raises(InputInconsistencyError) as caught:
        build_stored_input(
            bundle=_bundle(canonical_prompt_sha256="c" * 64, addenda_sha256=["a" * 64]),
            facts=_facts(),
            canonical_text=CANONICAL_TEXT,
            addenda_texts=ADDENDA,
        )

    assert "declares canonical" in str(caught.value)
    assert "declares addenda" in str(caught.value)
