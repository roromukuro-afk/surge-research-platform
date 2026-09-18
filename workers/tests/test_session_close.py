"""The confirmed session close (D-261 / D-262), against fakes.

Predictions are made after the close and refer to the session's confirmed
close. What these pin: a close counts only once it was read after the session
end plus the feed's delay; today's bar during the session is refused, not
passed off as a close; the provenance travels with the price.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.entry.session_close import (
    SessionClose,
    SessionCloseNotConfirmed,
    SessionCloseUnavailable,
    assert_close_is_confirmed,
)
from surge.http_fetch import HttpResponse
from surge.providers import alpaca_historical as alpaca
from surge.providers.yahoo_finance import (
    CHART_URL,
    COOKIE_URL,
    CRUMB_URL,
    JST,
    YahooSession,
    session_close,
    tse_session_end,
    tse_symbol,
)

SEP17 = date(2026, 9, 17)
SEP18 = date(2026, 9, 18)


# ------------------------------------------------------------- the rule


def _close(fetched_at: datetime) -> SessionClose:
    return SessionClose(
        market_code="JP", symbol="7203.T", session_date=SEP17, close=Decimal("3034"), currency="JPY",
        session_closed_at=datetime(2026, 9, 17, 6, 30, tzinfo=UTC), fetched_at=fetched_at,
        provider="TEST", feed="test", basis="TEST", publication_delay=timedelta(minutes=20),
    )


def test_a_close_read_after_the_end_plus_the_delay_is_confirmed():
    assert_close_is_confirmed(_close(datetime(2026, 9, 17, 6, 50, tzinfo=UTC)))


def test_a_close_read_before_the_delay_has_passed_is_not():
    with pytest.raises(SessionCloseNotConfirmed, match="mid-session"):
        assert_close_is_confirmed(_close(datetime(2026, 9, 17, 6, 49, 59, tzinfo=UTC)))


def test_the_provenance_is_recorded():
    evidence = _close(datetime(2026, 9, 18, 5, 0, tzinfo=UTC)).recorded_evidence
    assert "PROVIDER=TEST" in evidence
    assert "SESSION_DATE=2026-09-17" in evidence
    assert "PUBLICATION_DELAY_SECONDS=1200" in evidence


# ------------------------------------------------------------- JP (Yahoo)


def test_tse_symbols_and_session_ends():
    assert tse_symbol("7203") == "7203.T"
    assert tse_symbol("130a") == "130A.T"
    assert tse_symbol("72030") == "7203.T"
    for bad in ("", "72", "7203.T", "ABCD"):
        with pytest.raises(ValueError):
            tse_symbol(bad)
    assert tse_session_end(SEP17) == datetime(2026, 9, 17, 6, 30, tzinfo=UTC)
    assert tse_session_end(date(2024, 11, 1)) == datetime(2024, 11, 1, 6, 0, tzinfo=UTC)


class _Response:
    def __init__(self, status=200, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, chart):
        self.chart = chart

    def get(self, url, params=None, timeout=None):
        if url.startswith(COOKIE_URL):
            return _Response(404)
        if url.startswith(CRUMB_URL):
            return _Response(text="crumb123")
        if url.startswith(CHART_URL):
            return _Response(payload=self.chart)
        return _Response(404)

    def close(self):
        pass


def _stamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 9, 0, tzinfo=JST).timestamp())


def _chart(*, today=SEP18, last_trade: datetime | None = None, currency="JPY"):
    """Bars for Sep 17 and Sep 18; the current trading period is ``today``'s."""

    regular_start = _stamp(today)
    regular_end = int(tse_session_end(today).timestamp())
    return {"chart": {"result": [{
        "meta": {"symbol": "7203.T", "currency": currency,
                 "regularMarketTime": int((last_trade or tse_session_end(today)).timestamp()),
                 "currentTradingPeriod": {"regular": {"start": regular_start, "end": regular_end}}},
        "timestamp": [_stamp(SEP17), _stamp(SEP18)],
        "indicators": {"quote": [{"close": [3034.0, 3011.0]}]},
    }]}}


def _yahoo(chart) -> YahooSession:
    return YahooSession(session_factory=lambda: _Session(chart), sleep=lambda _s: None)


def test_a_past_session_is_read_and_confirmed():
    close = session_close("7203.T", SEP17, session=_yahoo(_chart()),
                          clock=lambda: datetime(2026, 9, 18, 5, 0, tzinfo=UTC))

    assert close.close == Decimal("3034.0")
    assert close.session_closed_at == datetime(2026, 9, 17, 6, 30, tzinfo=UTC)
    assert "YAHOO_UNOFFICIAL_JSON_API" in close.recorded_evidence


def test_todays_bar_during_the_session_is_not_a_close():
    with pytest.raises(SessionCloseNotConfirmed):
        session_close("7203.T", SEP18, session=_yahoo(_chart(last_trade=datetime(2026, 9, 18, 4, 50, tzinfo=UTC))),
                      clock=lambda: datetime(2026, 9, 18, 5, 5, tzinfo=UTC))


def test_todays_close_is_confirmed_once_the_close_print_is_visible():
    close = session_close("7203.T", SEP18, session=_yahoo(_chart()),
                          clock=lambda: datetime(2026, 9, 18, 6, 51, tzinfo=UTC))

    assert close.close == Decimal("3011.0")


def test_a_thin_stock_without_a_closing_print_waits_twice_the_delay():
    chart = _chart(last_trade=datetime(2026, 9, 18, 5, 40, tzinfo=UTC))
    with pytest.raises(SessionCloseNotConfirmed, match="not visible yet"):
        session_close("7203.T", SEP18, session=_yahoo(chart),
                      clock=lambda: datetime(2026, 9, 18, 6, 55, tzinfo=UTC))
    assert session_close("7203.T", SEP18, session=_yahoo(chart),
                         clock=lambda: datetime(2026, 9, 18, 7, 11, tzinfo=UTC)).close == Decimal("3011.0")


def test_a_missing_session_or_a_foreign_currency_is_unavailable():
    later = lambda: datetime(2026, 9, 20, tzinfo=UTC)  # noqa: E731
    with pytest.raises(SessionCloseUnavailable, match="no 2026-09-16 close"):
        session_close("7203.T", date(2026, 9, 16), session=_yahoo(_chart()), clock=later)
    with pytest.raises(SessionCloseUnavailable, match="not JPY"):
        session_close("7203.T", SEP17, session=_yahoo(_chart(currency="USD")), clock=later)


# ------------------------------------------------------------- US (Alpaca SIP)

CREDS = alpaca.Credentials(key_id="PKTESTTESTTESTTEST", secret_key="x" * 40)


def _bars_transport(received_at: datetime, bars=None):
    payload = {"bars": {"AAPL": bars if bars is not None else [
        {"t": "2026-09-17T04:00:00Z", "o": 335, "h": 339, "l": 334, "c": 337, "v": 1000, "n": 10, "vw": 336}
    ]}, "next_page_token": None, "currency": "USD"}

    def transport(url, headers=None, **kwargs):
        return HttpResponse(url=url, status=200, body=json.dumps(payload).encode(),
                            requested_at=received_at, received_at=received_at, headers={})

    return transport


def test_a_us_close_is_the_sip_daily_bar_read_after_the_close_plus_the_delay():
    read_at = datetime(2026, 9, 18, 5, 0, tzinfo=UTC)
    close = alpaca.session_close("AAPL", SEP17, now=read_at, credentials=CREDS,
                                 transport=_bars_transport(read_at))

    assert close.close == Decimal("337")
    assert close.session_closed_at == datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
    assert "FEED=sip" in close.recorded_evidence


def test_a_us_bar_read_before_the_close_plus_the_delay_is_not_a_close():
    read_at = datetime(2026, 9, 17, 20, 10, tzinfo=UTC)
    with pytest.raises(SessionCloseNotConfirmed):
        alpaca.session_close("AAPL", SEP17, now=read_at, credentials=CREDS, transport=_bars_transport(read_at))


def test_a_missing_us_bar_is_unavailable():
    read_at = datetime(2026, 9, 18, 5, 0, tzinfo=UTC)
    with pytest.raises(SessionCloseUnavailable, match="no 2026-09-17 SIP daily bar"):
        alpaca.session_close("AAPL", SEP17, now=read_at, credentials=CREDS,
                             transport=_bars_transport(read_at, bars=[]))
