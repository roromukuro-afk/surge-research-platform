"""The Alpaca historical adapter, and the three ways it could be wrong quietly.

A bad price provider does not throw. It returns plausible numbers that are a
little too low at the top, and the securities that reached +20% come back looking
like securities that did not. So most of these tests are about refusals: the feed
that must not be substituted, the window that must not be asked for, and the
backfill that must not start while the storage terms are unanswered.

The fixture is shaped from Alpaca's published response schema for
``GET /v2/stocks/bars``. The numbers are synthetic - CLAUDE.md allows only
synthetic fixtures - and the *shape* is the contract being tested.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from surge.http_fetch import HttpResponse
from surge.market.models import (
    PriceBasis,
    VenueBasis,
    VenueBasisError,
    assert_may_resolve_an_outcome,
)
from surge.models import Provenance
from surge.providers import alpaca_historical as alpaca
from surge.providers.base import ROLE_REALTIME_DECISION, assert_role_allowed

NOW = datetime(2026, 9, 17, 21, 30, tzinfo=UTC)
YESTERDAY_CLOSE = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)

#: Two sessions of one symbol, in the documented response shape: a map of symbol
#: to bars, each bar with t/o/h/l/c/v/n/vw, plus currency and a page token.
PAYLOAD = {
    "bars": {
        "SYNTH": [
            {
                "t": "2026-09-15T04:00:00Z",
                "o": 10.5,
                "h": 11.25,
                "l": 10.1,
                "c": 11.0,
                "v": 1_250_000,
                "n": 9_431,
                "vw": 10.83,
            },
            {
                "t": "2026-09-16T04:00:00Z",
                "o": 11.1,
                "h": 13.4,
                "l": 11.0,
                "c": 13.2,
                "v": 4_100_000,
                "n": 31_002,
                "vw": 12.61,
            },
        ]
    },
    "currency": "USD",
    "next_page_token": None,
}


def _provenance() -> Provenance:
    return Provenance(
        source_id=alpaca.PROVIDER_ID,
        endpoint="https://data.alpaca.markets/v2/stocks/bars",
        requested_at=NOW,
        received_at=NOW,
        http_status=200,
        bytes=len(json.dumps(PAYLOAD)),
        content_sha256="0" * 64,
        item_count=2,
        observed_at=NOW,
        available_at=NOW,
        dataset_key=alpaca.BARS_DATASET,
    )


# --------------------------------------------------------------- the feed


def test_the_request_always_names_the_consolidated_tape():
    """Not left to the default. A default that resolves against the caller's
    subscription is how a free key quietly returns one exchange."""

    url = alpaca.bars_url("SYNTH", start=date(2026, 9, 1), end=YESTERDAY_CLOSE, now=NOW)
    query = parse_qs(urlparse(url).query)

    assert query["feed"] == ["sip"]
    assert query["adjustment"] == ["raw"]


def test_another_feed_cannot_be_requested():
    with pytest.raises(alpaca.FeedError, match="consolidated tape"):
        alpaca.bars_url("SYNTH", start=date(2026, 9, 1), end=YESTERDAY_CLOSE, now=NOW, feed="iex")


def test_bars_fetched_on_another_feed_cannot_be_labelled_as_the_session():
    """Checking the caller's own argument, which is worth doing and is not the
    same as reading the response."""

    with pytest.raises(alpaca.FeedError, match="SINGLE_VENUE"):
        alpaca.to_canonical_bars(PAYLOAD, provenance=_provenance(), requested_feed="iex")


def test_the_basis_says_requested_because_the_response_names_no_feed():
    """Alpaca's bars response carries no feed identifier, so there is nothing to
    verify. What is known is that feed=sip was sent and the call succeeded, and
    recording that as verification would claim evidence nobody has."""

    bar = alpaca.to_canonical_bars(PAYLOAD, provenance=_provenance())[0]

    assert bar.venue_basis is VenueBasis.CONSOLIDATED_SIP_REQUESTED
    assert not bar.venue_basis.feed_was_verified_in_the_response
    assert bar.requested_feed == "sip"
    # Still whole-market evidence, and still allowed to resolve an outcome.
    assert bar.may_resolve_an_outcome


def test_a_response_that_does_name_its_feed_is_read_rather_than_ignored():
    """Forward compatibility with evidence: if Alpaca starts identifying the
    feed, the adapter should stop recording weaker evidence than it has."""

    named = dict(PAYLOAD, feed="sip")

    bar = alpaca.to_canonical_bars(named, provenance=_provenance())[0]

    assert bar.venue_basis is VenueBasis.CONSOLIDATED_SIP_VERIFIED
    assert bar.venue_basis.feed_was_verified_in_the_response


def test_a_response_naming_a_single_venue_is_refused():
    named = dict(PAYLOAD, feed="iex")

    with pytest.raises(alpaca.FeedError, match="one venue's"):
        alpaca.to_canonical_bars(named, provenance=_provenance())


# -------------------------------------------------------------- the window


def test_a_window_inside_the_delay_is_refused():
    too_recent = NOW - timedelta(minutes=5)

    with pytest.raises(alpaca.DelayedWindowError, match="15 minutes old"):
        alpaca.bars_url("SYNTH", start=date(2026, 9, 1), end=too_recent, now=NOW)


def test_the_boundary_is_the_documented_delay_plus_a_margin():
    limit = alpaca.latest_permitted_end(NOW)

    assert NOW - limit == alpaca.SIP_DELAY + alpaca.DELAY_MARGIN
    alpaca.check_window(limit, now=NOW)
    with pytest.raises(alpaca.DelayedWindowError):
        alpaca.check_window(limit + timedelta(seconds=1), now=NOW)


def test_a_naive_end_is_refused_rather_than_assumed_to_be_utc():
    with pytest.raises(alpaca.DelayedWindowError, match="timezone aware"):
        alpaca.check_window(datetime(2026, 9, 16, 20, 0), now=NOW)


def test_this_provider_cannot_be_bound_to_a_realtime_role():
    """A quarter of an hour late is not an entry price. D-103-LIVE is a separate
    question and binding this provider to it would answer it wrongly."""

    with pytest.raises(ValueError, match="REALTIME_DECISION"):
        assert_role_allowed(alpaca.capabilities(), ROLE_REALTIME_DECISION)


# ------------------------------------------------------------ the contract


def test_the_documented_response_shape_normalises():
    bars = alpaca.to_canonical_bars(PAYLOAD, provenance=_provenance())

    assert [bar.trade_date for bar in bars] == [date(2026, 9, 15), date(2026, 9, 16)]
    second = bars[1]
    assert second.native_symbol == "SYNTH"
    assert second.high == Decimal("13.4")
    assert second.volume == Decimal("4100000")
    assert second.trade_count == 31_002
    assert second.vwap == Decimal("12.61")
    assert second.currency == "USD"


def test_every_price_column_declares_itself_raw_and_consolidated():
    bar = alpaca.to_canonical_bars(PAYLOAD, provenance=_provenance())[0]

    assert bar.venue_basis.is_whole_market
    assert bar.close_basis is PriceBasis.RAW
    assert bar.may_resolve_an_outcome
    assert_may_resolve_an_outcome(bar)


def test_a_single_venue_bar_may_not_resolve_an_outcome():
    """The guard that makes the IEX finding structural rather than a memo."""

    from dataclasses import replace

    bar = replace(
        alpaca.to_canonical_bars(PAYLOAD, provenance=_provenance())[0],
        venue_basis=VenueBasis.SINGLE_VENUE_IEX,
    )

    assert not bar.may_resolve_an_outcome
    with pytest.raises(VenueBasisError, match="one venue's"):
        assert_may_resolve_an_outcome(bar)


def test_availability_is_now_not_the_trade_date():
    """A 2026-09-15 bar fetched today became available today. Anything else
    would let a backfill claim the system knew something before it did."""

    bar = alpaca.to_canonical_bars(PAYLOAD, provenance=_provenance())[0]

    assert bar.available_at == NOW
    assert bar.source_timestamp == datetime(2026, 9, 15, 4, 0, tzinfo=UTC)


def test_a_daily_bar_stamped_late_in_the_utc_day_is_refused():
    """If Alpaca moved the stamp to the session close, the UTC date would stop
    being the session date and every trade date would shift by one."""

    moved = {"bars": {"SYNTH": [dict(PAYLOAD["bars"]["SYNTH"][0], t="2026-09-15T20:00:00Z")]}}

    with pytest.raises(alpaca.AlpacaError, match="session open convention"):
        alpaca.to_canonical_bars(moved, provenance=_provenance())


def test_pagination_stops_on_a_null_token():
    assert alpaca.next_page_token(PAYLOAD) is None
    assert alpaca.next_page_token({"next_page_token": "abc"}) == "abc"
    assert alpaca.next_page_token({"next_page_token": ""}) is None


def test_the_symbol_lookup_date_can_be_pinned_to_the_window():
    """Alpaca resolves a ticker as of today by default, so a reassigned ticker
    would return the wrong company's history (CLAUDE.md 1-10b)."""

    url = alpaca.bars_url(
        "SYNTH", start=date(2020, 1, 2), end=YESTERDAY_CLOSE, now=NOW, asof=date(2020, 1, 2)
    )

    assert parse_qs(urlparse(url).query)["asof"] == ["2020-01-02"]


# ------------------------------------------------- credentials and the terms


def test_a_missing_credential_names_the_file_and_not_the_value():
    with pytest.raises(alpaca.CredentialsMissing, match=r"\.env\.local"):
        alpaca.Credentials.from_env({})


def test_a_credential_never_renders_itself():
    creds = alpaca.Credentials(key_id="PKTEST", secret_key="shhh")

    assert "shhh" not in repr(creds)
    assert "PKTEST" not in repr(creds)
    assert creds.redacted_headers["APCA-API-SECRET-KEY"] == "<redacted>"
    assert creds.headers["APCA-API-SECRET-KEY"] == "shhh"


def test_a_credential_smoke_is_allowed():
    alpaca.assert_smoke_sized(["SYNTH"], sessions=2)


def test_a_historical_backfill_is_refused_while_the_terms_are_unanswered():
    """NOT_SPECIFIED is not permission. Alpaca's Terms grant personal,
    noncommercial use and prohibit copying for publication, distribution or a
    commercial enterprise; a private research database is named by neither."""

    with pytest.raises(alpaca.PersistenceTermsUnconfirmed, match="NOT_SPECIFIED is not permission"):
        alpaca.assert_smoke_sized([f"SYN{i}" for i in range(50)], sessions=500)


def test_a_fetch_sends_the_key_headers_and_records_provenance():
    seen: dict = {}

    def transport(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        body = json.dumps(PAYLOAD).encode()
        return HttpResponse(
            url=url, status=200, body=body, requested_at=NOW, received_at=NOW, headers={}
        )

    payload, provenance = alpaca.fetch_bars(
        ["SYNTH"],
        start=date(2026, 9, 1),
        end=YESTERDAY_CLOSE,
        now=NOW,
        credentials=alpaca.Credentials(key_id="PKTEST", secret_key="shhh"),
        transport=transport,
    )

    assert payload["currency"] == "USD"
    assert provenance.dataset_key == alpaca.BARS_DATASET
    assert provenance.item_count == 2
    assert seen["headers"]["APCA-API-KEY-ID"] == "PKTEST"
    assert "feed=sip" in seen["url"]


# ---------------------------------------------- the smoke that keeps nothing


def _smoke_transport():
    def transport(url, **kwargs):
        return HttpResponse(
            url=url,
            status=200,
            body=json.dumps(PAYLOAD).encode(),
            requested_at=NOW,
            received_at=NOW,
            headers={},
        )

    return transport


def test_the_smoke_returns_a_report_and_not_the_bars():
    """Handing the bars back would make discarding them the caller's discipline,
    and discipline is not a guard."""

    report = alpaca.credential_smoke(
        ["SYNTH"],
        start=date(2026, 9, 14),
        end=YESTERDAY_CLOSE,
        now=NOW,
        credentials=alpaca.Credentials(key_id="PKTEST", secret_key="shhh"),
        transport=_smoke_transport(),
    )

    assert report.bars_returned == 2
    assert report.discarded is True
    assert report.venue_basis == "CONSOLIDATED_SIP_REQUESTED"
    assert report.requested_feed == "sip"
    # Counts, dates, hashes and labels only - no prices anywhere in the report.
    rendered = json.dumps(report.summary)
    for price in ("10.5", "11.25", "13.4", "13.2"):
        assert price not in rendered


def test_the_smoke_refuses_a_window_that_is_really_a_backfill():
    with pytest.raises(alpaca.PersistenceTermsUnconfirmed):
        alpaca.credential_smoke(
            [f"SYN{i}" for i in range(50)],
            start=date(2020, 1, 1),
            end=YESTERDAY_CLOSE,
            now=NOW,
            credentials=alpaca.Credentials(key_id="PKTEST", secret_key="shhh"),
            transport=_smoke_transport(),
        )


def test_the_smoke_report_says_why_nothing_was_kept():
    report = alpaca.credential_smoke(
        ["SYNTH"],
        start=date(2026, 9, 14),
        end=YESTERDAY_CLOSE,
        now=NOW,
        credentials=alpaca.Credentials(key_id="PKTEST", secret_key="shhh"),
        transport=_smoke_transport(),
    )

    assert "NOT_SPECIFIED is not permission" in report.summary["note"]
