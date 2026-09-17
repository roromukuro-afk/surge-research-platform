"""Alpaca historical bars, on the consolidated tape, deliberately late.

Alpaca's free Basic plan is two different things depending on which endpoint is
asked, and conflating them is what made the first reading of this provider
wrong. The distinction is the whole adapter:

*the latest endpoints*
    Real time. Basic gets IEX and nothing else. IEX is one exchange: Alpaca's own
    FAQ gives AAPL on 2023-09-29 as 12,630 trades on IEX against 535,134 across
    the tape. A session high built from that is not the session high.

*the historical endpoints*
    "For historical queries, the ``end`` parameter must be at least 15 minutes
    old to query SIP data without a subscription."
    (https://docs.alpaca.markets/us/docs/market-data-faq)

    SIP is the consolidated tape - every exchange, as the regulators require them
    to report. That is exactly the series outcome resolution needs, and it costs
    nothing as long as nothing is asked about the last quarter of an hour.

So this adapter serves end-of-day and history, and it refuses to serve anything
else. Two guards make that structural rather than advisory:

* ``feed`` is always ``sip``, passed explicitly on every request. The documented
  default is ``sip`` today, but a default that resolves against the caller's
  subscription is exactly the kind of thing that silently becomes ``iex`` on a
  free key, and the resulting bars would be wrong in the flattering direction.
* ``end`` is checked against the 15-minute delay before a request is built. Too
  recent is an error here rather than a 403 from the API, because the reason is
  worth stating in the caller's terms.

What this provider is *not* is the entry price. ``entry_reference_price`` is a
price that could have been traded at the moment a decision was made, and a feed
that is deliberately a quarter of an hour behind cannot supply one. US intraday
entry is a separate, still-unsolved question (D-103-LIVE); nothing here should be
read as progress on it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from surge.http_fetch import HttpResponse, fetch
from surge.licensing import AvailabilityBasis
from surge.market.models import CanonicalBar, PriceBasis, VenueBasis
from surge.models import Provenance, ProviderCapabilities
from surge.providers.base import (
    LATENCY_DELAYED_15M,
    ROLE_EOD_UNIVERSE,
    ROLE_INTRADAY_HISTORY,
)

PROVIDER_ID = "alpaca_historical_sip"
DATA_BASE = "https://data.alpaca.markets/v2"

BARS_DATASET = "ALPACA_US_BARS_SIP"

#: Never a variable, never taken from configuration, never left to the default.
#: The default is documented as ``sip`` and has historically resolved to whatever
#: the key is entitled to; on a free key that is ``iex``, which is one exchange.
FEED = "sip"

#: Raw, as traded. The eligibility filter and the outcome engine both need prices
#: that are what changed hands, not a series restated by a later split.
ADJUSTMENT = "raw"

#: "the ``end`` parameter must be at least 15 minutes old to query SIP data
#: without a subscription" - Alpaca Market Data FAQ.
SIP_DELAY = timedelta(minutes=15)

#: A small margin on top of the documented delay. Clock skew between this machine
#: and Alpaca's is not observable from here, and a request that is late by a
#: second is refused by the API rather than served from IEX - but being refused
#: in the middle of an end-of-day run is worse than waiting one more minute.
DELAY_MARGIN = timedelta(minutes=1)

TIMEFRAME_DAILY = "1Day"
MAX_LIMIT = 10_000

#: Until the persistence question below is answered, this adapter will fetch
#: enough to prove a credential works and no more. See ``docs/research/
#: alpaca-persistence-terms-2026-09-17.md``: Alpaca's Terms and Conditions grant
#: "personal and noncommercial access and use" and prohibit copying "for
#: publication or distribution or for any commercial enterprise" - which does not
#: name a private research database either way. NOT_SPECIFIED is not consent
#: (CLAUDE.md, quad-state licence vocabulary), so a smoke is allowed and a
#: backfill is not.
SMOKE_MAX_SYMBOLS = 5
SMOKE_MAX_SESSIONS = 10


class AlpacaError(RuntimeError):
    """Something about the request or the response was not what it claims."""


class DelayedWindowError(AlpacaError):
    """The window asked for reaches into the last fifteen minutes."""


class FeedError(AlpacaError):
    """A feed other than the consolidated tape was asked for or returned."""


class CredentialsMissing(AlpacaError):
    """No API key. The adapter is implemented; it has never spoken to Alpaca."""


class PersistenceTermsUnconfirmed(AlpacaError):
    """A bulk historical ingest, while the terms on storage are unanswered."""


@dataclass(frozen=True)
class Credentials:
    """Alpaca's documented header pair, read from the environment only.

    The values are never rendered: ``__repr__`` is suppressed on the secret and
    :meth:`redacted_headers` exists so a run log can say that authentication was
    present without saying what it was. CLAUDE.md forbids a key reaching chat, a
    log or the repository, and the cheapest way to keep that true is for the
    value never to be formattable.
    """

    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Credentials:
        source = env if env is not None else os.environ
        key_id = (source.get("APCA_API_KEY_ID") or "").strip()
        secret = (source.get("APCA_API_SECRET_KEY") or "").strip()
        if not key_id or not secret:
            raise CredentialsMissing(
                "APCA_API_KEY_ID and APCA_API_SECRET_KEY are not both set. Put them in .env.local "
                "(never in the repository, never in a chat message) and re-run. Until then this "
                "provider is IMPLEMENTED_NOT_LIVE_VERIFIED: the adapter and its contract tests "
                "exist and it has never called Alpaca"
            )
        return cls(key_id=key_id, secret_key=secret)

    @staticmethod
    def are_present(env: dict[str, str] | None = None) -> bool:
        source = env if env is not None else os.environ
        return bool(
            (source.get("APCA_API_KEY_ID") or "").strip()
            and (source.get("APCA_API_SECRET_KEY") or "").strip()
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    @property
    def redacted_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": "<redacted>",
            "APCA-API-SECRET-KEY": "<redacted>",
            "Accept": "application/json",
        }


def capabilities() -> ProviderCapabilities:
    """What this provider may be bound to.

    ``ROLE_REALTIME_DECISION`` is absent, and ``assert_role_allowed`` would
    refuse it anyway on the latency class. That refusal is the point: a series
    that is fifteen minutes old cannot be an entry price, and the binding check
    is a better place to discover that than an audit a year later.
    """

    return ProviderCapabilities(
        provider_id=PROVIDER_ID,
        markets=("US",),
        roles=(ROLE_EOD_UNIVERSE, ROLE_INTRADAY_HISTORY),
        update_schedule="continuous, readable from 15 minutes ago backwards",
        latency_class=LATENCY_DELAYED_15M,
        license_scope="PRIVATE_PERSONAL_RESEARCH_ONLY",
        provides_delisted=False,
    )


# ---------------------------------------------------------------- the window


def latest_permitted_end(now: datetime) -> datetime:
    """The most recent instant a free SIP query may ask about."""

    return now - SIP_DELAY - DELAY_MARGIN


def check_window(end: datetime, *, now: datetime) -> None:
    """Refuse a window that runs into the delayed quarter hour.

    Stated as its own function so that a caller which computes ``end`` from a
    session close can check it before doing any other work, and so the reason is
    a sentence rather than an HTTP status.
    """

    if end.tzinfo is None:
        raise DelayedWindowError("end must be timezone aware; a naive instant has no delay to check")
    limit = latest_permitted_end(now)
    if end > limit:
        raise DelayedWindowError(
            f"end {end.isoformat()} is inside the delayed window. Free SIP access requires end to be "
            f"at least {int(SIP_DELAY.total_seconds() // 60)} minutes old; the latest permitted end "
            f"right now is {limit.isoformat()}. Asking for anything more recent either fails or "
            "falls back to a single venue, and a single venue's high is not the session's"
        )


# ---------------------------------------------------------------- requests


def bars_url(
    symbols,
    *,
    start: date | datetime,
    end: datetime,
    now: datetime,
    timeframe: str = TIMEFRAME_DAILY,
    limit: int = MAX_LIMIT,
    page_token: str | None = None,
    asof: date | None = None,
    feed: str = FEED,
) -> str:
    """Build one historical bars request.

    ``asof`` is passed through deliberately. Alpaca resolves a symbol as of the
    current day by default, which means a ticker that has since been reassigned
    to another company would return the wrong company's history - the exact
    failure CLAUDE.md 1-10b is about. A backfill should pass the date it is
    asking about.
    """

    if feed != FEED:
        raise FeedError(
            f"feed={feed!r} was requested. This adapter exists to read the consolidated tape; a "
            "single-venue series cannot answer whether a session reached +20%, and silently "
            "accepting one here would put those bars in the same table as the real ones"
        )
    check_window(end, now=now)
    if not 1 <= limit <= MAX_LIMIT:
        raise AlpacaError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")

    names = [symbols] if isinstance(symbols, str) else list(symbols)
    if not names:
        raise AlpacaError("no symbols requested")

    params = {
        "symbols": ",".join(names),
        "timeframe": timeframe,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": str(limit),
        # Both are explicit for the same reason: a default that changes with the
        # plan or the release is a default this project cannot audit.
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "sort": "asc",
    }
    if asof is not None:
        params["asof"] = asof.isoformat()
    if page_token:
        params["page_token"] = page_token
    return f"{DATA_BASE}/stocks/bars?{urlencode(params)}"


def assert_smoke_sized(symbols, *, sessions: int) -> None:
    """Refuse a bulk backfill while the storage terms are NOT_SPECIFIED.

    Alpaca's Terms grant personal, noncommercial use and prohibit copying for
    publication, distribution or commercial enterprise. A private research
    database is neither permitted nor prohibited by name. The project's rule for
    that state is that silence is not consent, so the adapter is allowed to prove
    a credential works and is not allowed to start accumulating history.
    """

    names = [symbols] if isinstance(symbols, str) else list(symbols)
    if len(names) > SMOKE_MAX_SYMBOLS or sessions > SMOKE_MAX_SESSIONS:
        raise PersistenceTermsUnconfirmed(
            f"{len(names)} symbol(s) x {sessions} session(s) is a historical ingest, not a "
            "credential smoke. Alpaca's Terms and Conditions do not say whether market data may be "
            "kept in a private research database; NOT_SPECIFIED is not permission. Answer that "
            "first (docs/research/alpaca-persistence-terms-2026-09-17.md), then raise this limit in "
            "a versioned change"
        )


def fetch_bars(
    symbols,
    *,
    start: date | datetime,
    end: datetime,
    now: datetime | None = None,
    credentials: Credentials | None = None,
    timeframe: str = TIMEFRAME_DAILY,
    limit: int = MAX_LIMIT,
    page_token: str | None = None,
    asof: date | None = None,
    transport=fetch,
) -> tuple[dict[str, Any], Provenance]:
    """One request. Credentials are required and are never logged."""

    now = now or datetime.now(UTC)
    creds = credentials or Credentials.from_env()
    url = bars_url(
        symbols,
        start=start,
        end=end,
        now=now,
        timeframe=timeframe,
        limit=limit,
        page_token=page_token,
        asof=asof,
    )
    response: HttpResponse = transport(url, headers=creds.headers)
    if response.status != 200:
        raise AlpacaError(f"alpaca returned {response.status} for a bars request")
    payload = json.loads(response.body.decode("utf-8"))
    provenance = Provenance(
        source_id=PROVIDER_ID,
        endpoint=url,
        requested_at=response.requested_at,
        received_at=response.received_at,
        http_status=response.status,
        bytes=response.bytes,
        content_sha256=response.sha256,
        item_count=sum(len(rows) for rows in (payload.get("bars") or {}).values()),
        observed_at=response.received_at,
        # OBSERVED_NOW, always. A 2019 bar fetched today became available today;
        # claiming otherwise is how a backfill pretends to be a replay.
        available_at=response.received_at,
        dataset_key=BARS_DATASET,
    )
    return payload, provenance


# ------------------------------------------------------------ normalisation


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _parse_timestamp(raw: str) -> datetime:
    text = raw.replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def session_date_of(moment: datetime, *, timeframe: str) -> date:
    """The trading date a daily bar's timestamp refers to.

    Alpaca stamps a daily bar at the session open in UTC: 05:00Z under Eastern
    Standard Time, 04:00Z under Eastern Daylight Time. Both land on the same
    calendar day as the session, so the UTC date is the session date - but only
    because the hour is small. The assertion is there so that if Alpaca ever
    moves the stamp to, say, the session close, this fails loudly instead of
    shifting every trade date by one day.
    """

    moment = moment.astimezone(UTC)
    if timeframe == TIMEFRAME_DAILY and moment.hour >= 12:
        raise AlpacaError(
            f"a daily bar timestamped {moment.isoformat()} is stamped in the second half of the UTC "
            "day. The adapter assumes the US session open convention (04:00Z/05:00Z), under which "
            "the UTC date is the session date; that assumption no longer holds and the trade dates "
            "would be wrong by a day"
        )
    return moment.date()


def to_canonical_bars(
    payload: dict[str, Any],
    *,
    provenance: Provenance,
    timeframe: str = TIMEFRAME_DAILY,
    market_code: str = "US",
    feed: str = FEED,
) -> list[CanonicalBar]:
    """Turn one response into canonical bars, refusing a non-SIP payload.

    The feed is checked again here even though the request fixed it. A response
    is a separate fact from a request: a plan downgrade, a fallback, or a proxy
    could return IEX bars to a SIP query, and the failure would be invisible in
    the numbers.
    """

    if feed != FEED:
        raise FeedError(
            f"a response on feed {feed!r} cannot be stored as consolidated session data. "
            "Mark it SINGLE_VENUE and keep it out of the outcome path, or discard it"
        )

    currency = payload.get("currency") or "USD"
    bars: list[CanonicalBar] = []
    for symbol, rows in sorted((payload.get("bars") or {}).items()):
        for row in rows or []:
            stamped_at = _parse_timestamp(row["t"])
            bars.append(
                CanonicalBar(
                    provider_id=PROVIDER_ID,
                    dataset_key=BARS_DATASET,
                    market_code=market_code,
                    native_symbol=symbol,
                    trade_date=session_date_of(stamped_at, timeframe=timeframe),
                    currency=currency,
                    open=_decimal(row.get("o")),
                    high=_decimal(row.get("h")),
                    low=_decimal(row.get("l")),
                    close=_decimal(row.get("c")),
                    volume=_decimal(row.get("v")),
                    trade_count=int(row["n"]) if row.get("n") is not None else None,
                    vwap=_decimal(row.get("vw")),
                    venue_basis=VenueBasis.CONSOLIDATED_SIP,
                    # adjustment=raw was sent, so every column is as traded.
                    open_basis=PriceBasis.RAW,
                    high_basis=PriceBasis.RAW,
                    low_basis=PriceBasis.RAW,
                    close_basis=PriceBasis.RAW,
                    volume_basis=PriceBasis.RAW,
                    source_timestamp=stamped_at,
                    observed_at=provenance.observed_at,
                    available_at=provenance.available_at,
                    availability_basis=AvailabilityBasis.OBSERVED_NOW,
                )
            )
    return bars


def next_page_token(payload: dict[str, Any]) -> str | None:
    """Alpaca returns ``null`` when there is no more, and the caller must stop.

    Worth a function because the failure mode of getting this wrong is an
    infinite loop against a rate-limited API on somebody's free key.
    """

    token = payload.get("next_page_token")
    return token or None


__all__ = [
    "ADJUSTMENT",
    "BARS_DATASET",
    "DATA_BASE",
    "FEED",
    "PROVIDER_ID",
    "SIP_DELAY",
    "SMOKE_MAX_SESSIONS",
    "SMOKE_MAX_SYMBOLS",
    "AlpacaError",
    "CredentialsMissing",
    "Credentials",
    "DelayedWindowError",
    "FeedError",
    "PersistenceTermsUnconfirmed",
    "assert_smoke_sized",
    "bars_url",
    "capabilities",
    "check_window",
    "fetch_bars",
    "latest_permitted_end",
    "next_page_token",
    "session_date_of",
    "to_canonical_bars",
]
