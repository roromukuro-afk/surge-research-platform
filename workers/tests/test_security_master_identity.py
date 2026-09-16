"""Security master identity: ticker changes, delisting, duplicates, as-of reads.

These cover the Phase 1 fixtures the audit asked for. The database functions do
the final materialisation, so the checks here are about the identity rules the
worker and SQL agree on.
"""

from __future__ import annotations

from surge.ids import deterministic_uuid, issuer_key, listing_key, security_key
from surge.jobs.universe_sync import snapshot_rows
from surge.normalize import normalize_name
from surge.providers.nasdaq_trader import parse_symbol_directory
from surge.sql_emit import SNAPSHOT_COLUMNS

NASDAQ_HEADER = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"
OTHER_HEADER = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"


def test_identity_is_stable_across_runs():
    first = deterministic_uuid(security_key("US", "XNAS", "AAPL"))
    second = deterministic_uuid(security_key("US", "XNAS", "AAPL"))
    assert first == second


def test_identity_differs_per_exchange_and_market():
    assert deterministic_uuid(security_key("US", "XNAS", "AAPL")) != deterministic_uuid(
        security_key("US", "XNYS", "AAPL")
    )
    assert deterministic_uuid(security_key("JP", "XTKS", "13010")) != deterministic_uuid(
        security_key("US", "XTKS", "13010")
    )


def test_ticker_change_keeps_the_same_listing_identity():
    """A renamed ticker on the same listing slot keeps its listing id.

    The listing is keyed by exchange and local code, so a pure symbol change is
    recorded as ticker history rather than as a new security.
    """

    listing_id = deterministic_uuid(listing_key("XTKS", "13010"))
    assert listing_id == deterministic_uuid(listing_key("XTKS", "13010"))


def test_same_issuer_multiple_listings_share_issuer_identity():
    issuer_a = deterministic_uuid(issuer_key("US", normalize_name("Example Inc. Common Stock")))
    issuer_b = deterministic_uuid(issuer_key("US", normalize_name("EXAMPLE INC.")))
    assert issuer_a == issuer_b


def test_normalize_name_does_not_merge_different_issuers():
    assert normalize_name("Alpha Holdings") != normalize_name("Beta Holdings")


def test_duplicate_provider_records_are_counted():
    from datetime import UTC, datetime

    from surge.jobs.universe_sync import SyncResult, resolve_identities
    from surge.models import Provenance, RawSecurityRecord, UniverseDecision

    now = datetime.now(UTC)
    provenance = Provenance(
        source_id="fixture",
        endpoint="fixture://",
        requested_at=now,
        received_at=now,
        http_status=200,
        bytes=0,
        content_sha256="abc",
        item_count=2,
        observed_at=now,
        available_at=now,
    )
    duplicate = RawSecurityRecord(
        source_id="fixture",
        source_record_id="dup-1",
        market_code="US",
        exchange_id="XNAS",
        local_code="AAA",
        name="Dup Inc. Common Stock",
        security_type="COMMON_STOCK",
        symbol="AAA",
        currency="USD",
        country="US",
        cik="1",
    )
    second = RawSecurityRecord(**{**duplicate.__dict__, "source_record_id": "dup-2"})
    issuers, securities, _ = resolve_identities([duplicate, second])
    result = SyncResult(
        run_id=deterministic_uuid("run|test"),
        market_code="US",
        as_of_date=now.date(),
        universe_version="universe-1.0.0",
        records=[duplicate, second],
        decisions=[UniverseDecision("INCLUDED", "TARGET_MARKET_COMMON_STOCK")] * 2,
        provenances=[provenance],
        errors=[],
        issuer_identities=issuers,
        security_identities=securities,
    )

    assert result.duplicate_count == 1
    rows = snapshot_rows(result)
    assert len(rows) == 2
    assert len(rows[0]) == len(SNAPSHOT_COLUMNS)


def test_unknown_exchange_record_is_retained_for_as_of_reconstruction():
    """Records with an unresolved exchange still reach the snapshot.

    They carry no listing id, so ``ref.listings_as_of`` will not invent a listing
    for them, but they remain visible in coverage.
    """

    other = "\n".join([OTHER_HEADER, "XYZ|Mystery Listing|Q|XYZ|N|100|N|XYZ"])
    records, errors = parse_symbol_directory(NASDAQ_HEADER, other)
    assert len(records) == 1
    assert records[0].exchange_id is None
    assert errors
