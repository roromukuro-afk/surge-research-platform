"""EOD Prediction terms (eod-prediction-1.0.0, D-267), in Python.

S0's confirmed close is the reference; the 3,000 yen filter is judged on it;
the target is the close x 1.20; evaluation starts at S1 and is checked at
T+1/3/5/10/20. The database enforces the same (test_db_eod_predictions).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.entry.eod_prediction import (
    CHECKPOINT_SESSIONS,
    EOD_PREDICTION_RULE_VERSION,
    EodPredictionRefused,
    EodPriceLimitExceeded,
    eod_prediction_terms,
)
from surge.entry.session_close import SessionClose, SessionCloseNotConfirmed

S0 = date(2026, 9, 17)
JP_END = datetime(2026, 9, 17, 6, 30, tzinfo=UTC)
US_END = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)


def _jp(close="2950", fetched=JP_END + timedelta(minutes=25)) -> SessionClose:
    return SessionClose(
        market_code="JP", symbol="7203.T", session_date=S0, close=Decimal(close), currency="JPY",
        session_closed_at=JP_END, fetched_at=fetched, provider="YAHOO", feed="yahoo-daily-chart",
        basis="YAHOO_DAILY_BAR_RAW_CLOSE", publication_delay=timedelta(minutes=20),
    )


def _us(close="18.50") -> SessionClose:
    return SessionClose(
        market_code="US", symbol="XYZ", session_date=S0, close=Decimal(close), currency="USD",
        session_closed_at=US_END, fetched_at=US_END + timedelta(minutes=20), provider="ALPACA",
        feed="sip", basis="CONSOLIDATED_SIP_REQUESTED", publication_delay=timedelta(minutes=16),
    )


def _terms(close, **overrides):
    kwargs = {
        "security_id": "sec-1",
        "data_cutoff": close.session_closed_at + timedelta(minutes=30),
        "decision_completed_at": close.fetched_at + timedelta(minutes=30),
    }
    kwargs.update(overrides)
    return eod_prediction_terms(close, **kwargs)


def test_the_terms_follow_the_rules():
    terms = _terms(_jp())

    assert terms.signal_reference_price == Decimal("2950")
    assert terms.signal_price_jpy == Decimal("2950")
    assert terms.target_price == Decimal("3540.000000")
    assert terms.evaluation_start_session == 1
    assert terms.checkpoint_sessions == CHECKPOINT_SESSIONS == (1, 3, 5, 10, 20)
    assert terms.horizon_sessions == 20
    assert terms.rule_version == EOD_PREDICTION_RULE_VERSION
    assert "PROVIDER=YAHOO" in terms.close_evidence


def test_the_filter_is_judged_on_the_s0_close():
    assert _terms(_jp("3000")).signal_price_jpy == Decimal("3000")
    with pytest.raises(EodPriceLimitExceeded):
        _terms(_jp("3000.1"))


def test_a_us_close_is_converted_with_a_rate_from_no_later_than_the_close():
    at = US_END - timedelta(hours=6)
    terms = _terms(_us("18.50"), fx_rate=Decimal("150.25"), fx_observed_at=at)
    assert terms.signal_price_jpy == Decimal("2779.625000")
    assert terms.target_price == Decimal("22.200000")

    with pytest.raises(EodPriceLimitExceeded):
        _terms(_us("25"), fx_rate=Decimal("150.25"), fx_observed_at=at)
    with pytest.raises(EodPredictionRefused, match="after the S0 close"):
        _terms(_us(), fx_rate=Decimal("150.25"), fx_observed_at=US_END + timedelta(seconds=1))
    with pytest.raises(EodPredictionRefused, match="needs a USD/JPY rate"):
        _terms(_us())


def test_a_yen_close_is_not_converted():
    with pytest.raises(EodPredictionRefused, match="not converted"):
        _terms(_jp(), fx_rate=Decimal("1"), fx_observed_at=JP_END)


def test_a_close_that_is_not_confirmed_makes_no_prediction():
    with pytest.raises(SessionCloseNotConfirmed):
        _terms(_jp(fetched=JP_END + timedelta(minutes=19)),
               decision_completed_at=JP_END + timedelta(hours=1))


def test_the_prediction_is_made_after_the_close_was_confirmed():
    close = _jp()
    with pytest.raises(EodPredictionRefused, match="before its close was confirmed"):
        _terms(close, decision_completed_at=close.fetched_at - timedelta(seconds=1),
               data_cutoff=JP_END)
    with pytest.raises(EodPredictionRefused, match="data_cutoff"):
        _terms(close, data_cutoff=JP_END - timedelta(seconds=1))
