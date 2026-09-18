"""The entry reference price: observed after the decision, or not at all.

The decision is made against ``decision_price``, which the model saw. Scoring
uses ``entry_reference_price``, which is what could realistically have been
traded once the decision existed. Those are different moments and the order
between them is the whole point.

The price used to be an argument to the run, which meant it had been observed
*before* the analysis started - the most favourable possible reading, handed in
by the caller. These pin the replacement: it is fetched afterwards, it is
checked against the decision's own clock, and failing to get one is a recorded
outcome rather than an exception that loses the decision.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from surge.entry.models import ObservedPrice
from surge.entry.price_observer import (
    ENTRY_PRICE_RECOVERY_DELAYED,
    NO_ENTRY_REFERENCE_PRICE,
    EntryPriceObservation,
    EntryPriceUnavailable,
    NoEntryPriceSource,
    assert_observation_is_usable,
)

COMPLETED = datetime(2026, 9, 17, 2, 17, tzinfo=UTC)


def _observation(at: datetime, amount="1005") -> EntryPriceObservation:
    return EntryPriceObservation(
        price=ObservedPrice(amount=Decimal(amount), currency="JPY", observed_at=at),
        method="LAST_TRADE",
    )


def test_a_price_from_before_the_decision_is_refused():
    """The error this exists to prevent looks like good luck rather than like a
    bug: every entry priced before its own decision is priced at whatever the
    market was doing while the model was still thinking."""

    with pytest.raises(EntryPriceUnavailable, match="before the decision"):
        assert_observation_is_usable(
            _observation(COMPLETED - timedelta(seconds=1)),
            decision_completed_at=COMPLETED,
        )


def test_a_price_at_the_moment_of_completion_is_allowed():
    """The boundary is inclusive. A price stamped at the same instant is not
    evidence of having seen the future."""

    assert_observation_is_usable(_observation(COMPLETED), decision_completed_at=COMPLETED)


def test_a_later_price_is_allowed():
    assert_observation_is_usable(
        _observation(COMPLETED + timedelta(seconds=30)), decision_completed_at=COMPLETED
    )


def test_the_default_source_refuses_rather_than_inventing():
    """No intraday source is bound (D-06b / D-103-LIVE). A runner that made one
    up would produce predictions scored against a price nothing observed, which
    is worse than having no prediction."""

    with pytest.raises(EntryPriceUnavailable, match="no intraday entry price source"):
        NoEntryPriceSource().observe(
            security_id="sec-1", market_code="JP", not_before=COMPLETED
        )


def test_the_refusal_names_the_decisions_that_would_settle_it():
    try:
        NoEntryPriceSource().observe(
            security_id="sec-1", market_code="JP", not_before=COMPLETED
        )
    except EntryPriceUnavailable as exc:
        assert "D-06b" in str(exc)
        assert "D-103-LIVE" in str(exc)
    else:  # pragma: no cover - the call above always raises
        raise AssertionError("the default source is supposed to refuse")


def test_the_two_reason_codes_are_distinct():
    """One says the decision could not be priced; the other says it was priced
    late. Collapsing them would hide a real gap between the decision and the
    price behind an absence."""

    assert NO_ENTRY_REFERENCE_PRICE != ENTRY_PRICE_RECOVERY_DELAYED
    assert NO_ENTRY_REFERENCE_PRICE == "NO_ENTRY_REFERENCE_PRICE"
    assert ENTRY_PRICE_RECOVERY_DELAYED == "ENTRY_PRICE_RECOVERY_DELAYED"


def test_an_observation_carries_the_method_that_produced_it():
    """D-01a is not settled. Recording the method per observation is what lets
    it be settled later without invalidating what was recorded before."""

    observation = _observation(COMPLETED)

    assert observation.method == "LAST_TRADE"
    assert observation.amount == Decimal("1005")
    assert observation.observed_at == COMPLETED
