"""Yahoo Finance: the confirmed session close for Tokyo listings (D-262).

Where this comes from. The request logic is the user's own screener's
(``mea-stock-screener``, ``lib/provider.py``), reused under an explicit,
one-time exception to CLAUDE.md 1-1 given on 2026-09-18 for personal research
use: a browser-like HTTPS session (``curl_cffi`` impersonating Chrome), the
public cookie from ``fc.yahoo.com``, a crumb, then Yahoo's JSON chart endpoint.
No web page is scraped.

What it is, because that decides what it may be used for:

* **Not a documented or licensed API.** Yahoo may change it without notice;
  every close carries its provider, feed and read time, so a change breaks
  loudly rather than silently.
* **Delayed about twenty minutes for Tokyo** (``exchangeDataDelayedBy: 20``;
  measured on 2026-09-18 the newest print was about fifteen minutes old). That
  does not matter for a close read after the session: the close is confirmed
  once the session has ended *and* the delay has passed, and not before.
* The daily bar's close is the raw traded close (``indicators.quote``), not
  the adjusted series.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote as url_quote

from surge.entry.session_close import (
    SessionClose,
    SessionCloseNotConfirmed,
    SessionCloseUnavailable,
    assert_close_is_confirmed,
)

PROVIDER = "YAHOO"
FEED = "yahoo-daily-chart"
BASIS = "YAHOO_DAILY_BAR_RAW_CLOSE"
#: What Yahoo declares for Tokyo (``exchangeDataDelayedBy``, minutes).
TSE_PUBLICATION_DELAY = timedelta(minutes=20)
#: Japan has no daylight saving time, so a fixed offset is exact.
JST = timezone(timedelta(hours=9), "JST")
#: The TSE regular session has ended at 15:30 since 2024-11-05, and at 15:00 before.
TSE_CLOSE_EXTENDED_ON = date(2024, 11, 5)

COOKIE_URL = "https://fc.yahoo.com"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"


class YahooError(RuntimeError):
    """Yahoo could not be read, or answered something that cannot be used."""


def tse_symbol(local_code: str) -> str:
    """The Yahoo symbol for a JPX local code: ``7203`` -> ``7203.T``.

    Accepts the four-character code (including the newer alphanumeric ones,
    ``130A``) and the five-character form with a trailing check digit ``0``
    (``72030``). Anything else is refused rather than guessed - a ticker is not
    an identity (CLAUDE.md 1-10b), and a malformed one would price some other
    security.
    """

    code = (local_code or "").strip().upper()
    if len(code) == 5 and code.endswith("0"):
        code = code[:4]
    if len(code) != 4 or not code[0].isdigit() or not code.isalnum() or not code.isascii():
        raise ValueError(f"{local_code!r} is not a JPX local code")
    return f"{code}.T"


def tse_session_end(session_date: date) -> datetime:
    hour, minute = (15, 30) if session_date >= TSE_CLOSE_EXTENDED_ON else (15, 0)
    return datetime(session_date.year, session_date.month, session_date.day, hour, minute, tzinfo=JST).astimezone(UTC)


def _chrome_session() -> Any:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise YahooError("curl_cffi is not installed; install the worker with the yahoo extra") from exc
    return curl_requests.Session(impersonate="chrome")


@dataclass
class YahooSession:
    """One cookie-and-crumb session, as a browser gets it. Renewed after a failure."""

    session_factory: Callable[[], Any] = _chrome_session
    timeout: float = 15.0
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    _session: Any = field(default=None, init=False, repr=False)
    _crumb: str | None = field(default=None, init=False, repr=False)

    def reset(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        self._session, self._crumb = None, None

    def _get(self, url: str, params: dict | None = None):
        for attempt in range(2):
            response = self._session.get(url, params=params, timeout=self.timeout)
            if (response.status_code == 429 or response.status_code >= 500) and attempt == 0:
                self.sleep(2)
                continue
            if response.status_code >= 400:
                raise YahooError(f"yahoo returned HTTP {response.status_code} for {url.split('?')[0]}")
            return response
        raise YahooError(f"yahoo did not answer {url.split('?')[0]}")  # pragma: no cover

    def _ensure(self) -> None:
        if self._session is not None:
            return
        self._session = self.session_factory()
        # fc.yahoo.com answers 404 but sets the public cookie the API expects.
        self._session.get(COOKIE_URL, timeout=self.timeout)
        crumb = self._get(CRUMB_URL).text
        if not crumb or len(crumb) > 100 or "<" in crumb:
            self.reset()
            raise YahooError("no usable crumb, so no session could be started")
        self._crumb = crumb

    def chart(self, symbol: str, params: dict) -> dict:
        self._ensure()
        return self._get(CHART_URL + url_quote(symbol, safe=""), params=params).json()


def session_close(
    symbol: str,
    session_date: date,
    *,
    session: YahooSession | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SessionClose:
    """The confirmed close of ``session_date`` for a Tokyo ``symbol`` (``7203.T``).

    Raises :class:`SessionCloseNotConfirmed` when the session has not ended plus
    the delay, or when it is today's session and the close print has not been
    published yet; :class:`SessionCloseUnavailable` when there is no bar.
    """

    session = session or YahooSession()
    day_start = datetime(session_date.year, session_date.month, session_date.day, tzinfo=JST)
    params = {
        "interval": "1d",
        "period1": str(int((day_start - timedelta(days=1)).timestamp())),
        "period2": str(int((day_start + timedelta(days=2)).timestamp())),
    }
    try:
        data = session.chart(symbol, params)
    except (TypeError, AttributeError, NameError):
        raise
    except Exception as exc:  # noqa: BLE001 - a third-party transport's errors, at the boundary
        session.reset()
        raise SessionCloseUnavailable(f"the Yahoo chart for {symbol} could not be read: {exc}") from exc
    fetched_at = clock()

    results = (data.get("chart") or {}).get("result") or []
    if not results:
        raise SessionCloseUnavailable(f"no Yahoo chart for {symbol}")
    result = results[0]
    meta = result.get("meta") or {}
    if meta.get("symbol") != symbol:
        raise SessionCloseUnavailable("the chart answered for a different symbol")
    if meta.get("currency") != "JPY":
        raise SessionCloseUnavailable(f"the chart for {symbol} is in {meta.get('currency')!r}, not JPY")

    stamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    close = None
    for stamp, value in zip(stamps, closes, strict=False):
        if datetime.fromtimestamp(stamp, JST).date() == session_date:
            close = value
    if close is None or isinstance(close, bool) or not isinstance(close, int | float) or close <= 0:
        raise SessionCloseUnavailable(f"no {session_date.isoformat()} close for {symbol}")

    session_end = tse_session_end(session_date)
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    is_current_session = (
        isinstance(regular.get("start"), int)
        and datetime.fromtimestamp(regular["start"], JST).date() == session_date
    )
    if is_current_session and isinstance(regular.get("end"), int):
        session_end = datetime.fromtimestamp(regular["end"], UTC)

    result_close = SessionClose(
        market_code="JP",
        symbol=symbol,
        session_date=session_date,
        close=Decimal(str(close)),
        currency="JPY",
        session_closed_at=session_end,
        fetched_at=fetched_at,
        provider=PROVIDER,
        feed=FEED,
        basis=BASIS,
        publication_delay=TSE_PUBLICATION_DELAY,
        evidence=("DELAYED_FEED", "YAHOO_UNOFFICIAL_JSON_API", "PERSONAL_RESEARCH_USE"),
    )
    assert_close_is_confirmed(result_close)
    if is_current_session:
        # Today's bar. The delay has passed; the closing print itself should be
        # visible too, or the bar may still hold a pre-close price. A thin stock
        # may simply not have traded at the close, so once twice the delay has
        # passed its last trade is taken as the close.
        last_trade = meta.get("regularMarketTime")
        published = isinstance(last_trade, int) and datetime.fromtimestamp(last_trade, UTC) >= session_end
        if not published and fetched_at < session_end + 2 * TSE_PUBLICATION_DELAY:
            raise SessionCloseNotConfirmed(
                f"the {session_date.isoformat()} close of {symbol} is not visible yet: the newest trade "
                "Yahoo shows is from before the session end"
            )
    return result_close


__all__ = [
    "BASIS",
    "FEED",
    "JST",
    "PROVIDER",
    "TSE_PUBLICATION_DELAY",
    "YahooError",
    "YahooSession",
    "session_close",
    "tse_session_end",
    "tse_symbol",
]
