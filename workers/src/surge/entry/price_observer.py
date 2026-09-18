"""Where the entry reference price comes from, and when.

The price a decision is *scored* against is not the price it was *made* from.
The decision is made against ``decision_price``, which the model saw; scoring
uses ``entry_reference_price``, which is what could realistically have been
traded once the decision existed (CLAUDE.md 1-4).

Those are different moments, and the order between them is the whole point. An
entry price observed before the decision finished is a price the decision could
not have acted on, and using one would make every entry look luckier than it
was - by exactly the amount the price moved while the model was thinking.

So the price is not an argument any more. It used to be passed into
``run_for_trigger`` alongside the bundle, which meant it had been observed
*before* the analysis started: the most favourable possible reading, handed in
by the caller. It is now fetched through this interface, after the analysis has
produced an ENTRY and after ``decision_completed_at`` has been fixed by the
runner's own clock.

Three consequences worth stating:

* **Only an ENTRY asks.** A REJECT, a WATCH or a reaffirmation does not need a
  price, and observing one costs a provider call for a number nothing uses.
* **Not observing it is a real outcome**, not an error:
  ``NO_ENTRY_REFERENCE_PRICE`` is an entry attempt status, so the ledger records
  that the decision happened and could not be priced.
* **The 3,000 yen filter is re-asked here.** The decision price passing it does
  not mean the entry price does, and D-31 says the second check happens on the
  price the entry would actually have been at.

The concrete method - last trade, next print, VWAP of the following minute - is
D-01a and is not settled. It is a property of the implementation behind this
interface, and every observation carries the ``method`` that produced it, so
choosing later does not invalidate what was recorded earlier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from surge.entry.models import EntryError, ObservedPrice

#: Reported when the decision was made and no price could be observed to score
#: it against. The attempt is still written: a decision that happened and could
#: not be priced is a different fact from a decision that did not happen.
NO_ENTRY_REFERENCE_PRICE = "NO_ENTRY_REFERENCE_PRICE"

#: Recorded as evidence when the price was observed during a recovery pass
#: rather than immediately after the decision. The gap is real and is not
#: hidden: the alternative is back-dating, which would claim the system could
#: have traded at a price it was not running to see.
ENTRY_PRICE_RECOVERY_DELAYED = "ENTRY_PRICE_RECOVERY_DELAYED"


class EntryPriceUnavailable(EntryError):
    """No price could be observed for this entry.

    An ``EntryError`` because it stops a prediction, and caught by the runner
    rather than propagated: the attempt is recorded as
    ``NO_ENTRY_REFERENCE_PRICE``.
    """


@dataclass(frozen=True)
class EntryPriceObservation:
    """One observation, with the method that produced it and when it was taken.

    ``observed_at`` is the provider's time for the price, not the time this
    process asked. The runner checks it against ``decision_completed_at``; a
    provider that returns a stale quote is caught there rather than trusted
    because the call happened afterwards.
    """

    price: ObservedPrice
    method: str
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def amount(self) -> Decimal:
        return self.price.amount

    @property
    def observed_at(self) -> datetime:
        return self.price.observed_at


class EntryPriceObserver(Protocol):
    """Observes the price an entry would realistically have been made at.

    Implementations are bound per market and per provider role
    (``INTRADAY_ENTRY_JP`` / ``INTRADAY_ENTRY_US``). None is bound today -
    D-06b for Japan and D-103-LIVE for the United States - which is why this is
    an interface with no production implementation rather than a wrapper around
    one that exists.
    """

    #: Named so a recorded observation says which rule produced it.
    method: str

    def observe(
        self, *, security_id: str, market_code: str, not_before: datetime
    ) -> EntryPriceObservation:
        """The price after ``not_before``, or raise :class:`EntryPriceUnavailable`.

        ``not_before`` is ``decision_completed_at``. An implementation must not
        return a price observed earlier than it - returning the last price it
        happens to hold is exactly the mistake this argument exists to prevent.
        """
        ...


@dataclass(frozen=True)
class NoEntryPriceSource:
    """The observer used while no intraday source is bound.

    It refuses rather than inventing, so a pipeline run before a provider exists
    records ``NO_ENTRY_REFERENCE_PRICE`` against a real decision instead of a
    prediction built on a fabricated price. That is the honest shape of "we do
    not have this yet", and it is also what the readiness report is reporting
    when it says the intraday role is unbound.
    """

    method: str = "NO_INTRADAY_SOURCE"
    blocker_id: str = "D-06b / D-103-LIVE"

    def observe(
        self, *, security_id: str, market_code: str, not_before: datetime
    ) -> EntryPriceObservation:
        raise EntryPriceUnavailable(
            f"no intraday entry price source is bound for {market_code} ({self.blocker_id}), so "
            f"no price after {not_before.isoformat()} can be observed for {security_id}"
        )


def assert_observation_is_usable(
    observation: EntryPriceObservation, *, decision_completed_at: datetime
) -> None:
    """The one check that cannot be delegated to the implementation.

    An observer that returned a price from before the decision would produce a
    prediction scored against a price it could not have traded at, and the error
    would look like good luck rather than like a bug.
    """

    if observation.observed_at < decision_completed_at:
        raise EntryPriceUnavailable(
            f"the entry price was observed at {observation.observed_at.isoformat()}, before the "
            f"decision completed at {decision_completed_at.isoformat()}. A price from before the "
            "decision is not a price the decision could have acted on"
        )


__all__ = [
    "ENTRY_PRICE_RECOVERY_DELAYED",
    "NO_ENTRY_REFERENCE_PRICE",
    "EntryPriceObservation",
    "EntryPriceObserver",
    "EntryPriceUnavailable",
    "NoEntryPriceSource",
    "assert_observation_is_usable",
]
