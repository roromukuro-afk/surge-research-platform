"""EOD Prediction terms (eod-prediction-1.0.0, D-267): the rules, before the database.

The user's rules of 2026-09-18, specified in ``docs/specs/eod-prediction.md``:

* S0 is the target session; the prediction is made after S0's close is
  confirmed, and its reference is that confirmed close (``signal_reference_price``).
* The 3,000 yen hard filter is judged on the S0 close.
* The target is the S0 close x 1.20.
* Evaluation starts at S1; the outcome is checked at T+1, T+3, T+5, T+10 and
  T+20, and T+20 is the deadline.

The database (``prod.eod_predictions``) enforces the same rules; this module is
where a job hears "no" first, with a sentence instead of a constraint name.
Whether +20% is reached on the intraday high or the close is undecided (D-268)
and is the outcome's business, not a term of the prediction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from surge.entry.models import PRICE_LIMIT_JPY, PRIMARY_HORIZON_SESSIONS, TARGET_MULTIPLE, EntryError
from surge.entry.session_close import SessionClose, assert_close_is_confirmed

EOD_PREDICTION_RULE_VERSION = "eod-prediction-1.0.0"
EVALUATION_START_SESSION = 1
CHECKPOINT_SESSIONS = (1, 3, 5, 10, 20)
HORIZON_SESSIONS = PRIMARY_HORIZON_SESSIONS
_SIX_PLACES = Decimal("0.000001")


class EodPredictionRefused(EntryError):
    """The terms do not allow a prediction; none is made."""


class EodPriceLimitExceeded(EodPredictionRefused):
    """The S0 close is above 3,000 yen."""


@dataclass(frozen=True)
class EodPredictionTerms:
    security_id: str
    market_code: str
    s0_session_date: date
    signal_reference_price: Decimal
    signal_price_currency: str
    signal_price_jpy: Decimal
    fx_rate: Decimal | None
    fx_observed_at: datetime | None
    s0_session_closed_at: datetime
    close_fetched_at: datetime
    close_publication_delay_seconds: int
    close_provider: str
    close_feed: str
    close_basis: str
    close_evidence: tuple[str, ...]
    data_cutoff: datetime
    decision_completed_at: datetime
    target_price: Decimal
    evaluation_start_session: int = EVALUATION_START_SESSION
    checkpoint_sessions: tuple[int, ...] = CHECKPOINT_SESSIONS
    horizon_sessions: int = HORIZON_SESSIONS
    rule_version: str = EOD_PREDICTION_RULE_VERSION


def eod_prediction_terms(
    close: SessionClose,
    *,
    security_id: str,
    data_cutoff: datetime,
    decision_completed_at: datetime,
    fx_rate: Decimal | None = None,
    fx_observed_at: datetime | None = None,
) -> EodPredictionTerms:
    """The terms of an after-close prediction on ``close``, or a refusal saying why."""

    assert_close_is_confirmed(close)
    if decision_completed_at < close.fetched_at:
        raise EodPredictionRefused(
            f"the prediction completed at {decision_completed_at.isoformat()}, before its close was "
            f"confirmed at {close.fetched_at.isoformat()}; it has to be made after the close"
        )
    if not close.session_closed_at <= data_cutoff <= decision_completed_at:
        raise EodPredictionRefused(
            f"data_cutoff {data_cutoff.isoformat()} has to be at or after the S0 session end "
            f"({close.session_closed_at.isoformat()}) and at or before the decision"
        )

    if close.currency == "JPY":
        if fx_rate is not None or fx_observed_at is not None:
            raise EodPredictionRefused("a yen close is not converted")
        jpy = close.close
    else:
        if fx_rate is None or fx_rate <= 0 or fx_observed_at is None:
            raise EodPredictionRefused(
                f"a {close.currency} close needs a USD/JPY rate for the 3,000 yen filter"
            )
        if fx_observed_at > close.session_closed_at:
            raise EodPredictionRefused(
                f"the rate was observed at {fx_observed_at.isoformat()}, after the S0 close; the "
                "filter may only use a rate from no later than the close (CLAUDE.md 1-7)"
            )
        jpy = (close.close * fx_rate).quantize(_SIX_PLACES, rounding=ROUND_HALF_UP)

    if jpy > PRICE_LIMIT_JPY:
        raise EodPriceLimitExceeded(
            f"the S0 close of {close.symbol} is {jpy} yen, above the {PRICE_LIMIT_JPY} yen limit"
        )

    return EodPredictionTerms(
        security_id=security_id,
        market_code=close.market_code,
        s0_session_date=close.session_date,
        signal_reference_price=close.close,
        signal_price_currency=close.currency,
        signal_price_jpy=jpy,
        fx_rate=fx_rate,
        fx_observed_at=fx_observed_at,
        s0_session_closed_at=close.session_closed_at,
        close_fetched_at=close.fetched_at,
        close_publication_delay_seconds=int(close.publication_delay.total_seconds()),
        close_provider=close.provider,
        close_feed=close.feed,
        close_basis=close.basis,
        close_evidence=close.recorded_evidence,
        data_cutoff=data_cutoff,
        decision_completed_at=decision_completed_at,
        target_price=(close.close * TARGET_MULTIPLE).quantize(_SIX_PLACES, rounding=ROUND_HALF_UP),
    )


__all__ = [
    "CHECKPOINT_SESSIONS",
    "EOD_PREDICTION_RULE_VERSION",
    "EVALUATION_START_SESSION",
    "HORIZON_SESSIONS",
    "EodPredictionRefused",
    "EodPredictionTerms",
    "EodPriceLimitExceeded",
    "eod_prediction_terms",
]
