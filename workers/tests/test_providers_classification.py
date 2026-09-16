"""Provider parsing and security type classification against real file layouts."""

from __future__ import annotations

from surge.providers.nasdaq_trader import classify_security_name, parse_symbol_directory

NASDAQ_HEADER = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"
OTHER_HEADER = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"


def test_classify_common_stock():
    assert classify_security_name("Apple Inc. - Common Stock", etf_flag="N")[0] == "COMMON_STOCK"


def test_classify_etf_by_flag():
    assert classify_security_name("Some Fund ETF", etf_flag="Y")[0] == "ETF"


def test_classify_warrant_right_unit_preferred():
    assert classify_security_name("Acme Corp - Warrant", etf_flag="N")[0] == "WARRANT"
    assert classify_security_name("Acme Corp - Rights", etf_flag="N")[0] == "RIGHT"
    assert classify_security_name("Acme Corp - Unit", etf_flag="N")[0] == "UNIT"
    assert classify_security_name("Acme Corp 6.5% Preferred Series A", etf_flag="N")[0] == "PREFERRED"


def test_classify_adr():
    security_type, is_adr, _ = classify_security_name(
        "Example plc American Depositary Shares", etf_flag="N"
    )
    assert security_type == "ADR"
    assert is_adr is True


def test_classify_unknown_stays_unknown():
    assert classify_security_name("Mystery Security", etf_flag="N")[0] == "UNKNOWN"


def test_classify_singular_ordinary_share():
    """Real listings use both "Ordinary Share" and "Ordinary Shares"."""

    assert classify_security_name("Aimei Health Technology Co., Ltd. Ordinary Share", etf_flag="N")[0] == "COMMON_STOCK"


def test_classify_ads_token_is_adr():
    security_type, is_adr, _ = classify_security_name("Agora, Inc. ADS", etf_flag="N")
    assert security_type == "ADR"
    assert is_adr is True


def test_company_name_containing_ads_is_not_an_adr():
    """"ADS-TEC Energy" is a company name, not an American Depositary Share."""

    security_type, is_adr, _ = classify_security_name("ADS-TEC Energy PLC Ordinary Shares", etf_flag="N")
    assert security_type == "COMMON_STOCK"
    assert is_adr is False


def test_classify_new_york_registry_shares_as_adr():
    assert classify_security_name("ASML Holding N.V. New York Registry Shares", etf_flag="N")[0] == "ADR"


def test_classify_closed_end_fund_as_fund():
    assert classify_security_name("Ares Capital Corporation Closed End Fund", etf_flag="N")[0] == "REIT_OR_FUND"


def test_parse_symbol_directory_maps_exchanges_and_flags():
    nasdaq = "\n".join(
        [
            NASDAQ_HEADER,
            "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N",
            "TEST|Nasdaq Test Issue - Common Stock|Q|Y|N|100|N|N",
            "File Creation Time: 0916202512:00",
        ]
    )
    other = "\n".join(
        [
            OTHER_HEADER,
            "A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A",
            "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY",
            "XYZ|Mystery Listing|Q|XYZ|N|100|N|XYZ",
        ]
    )

    records, errors = parse_symbol_directory(nasdaq, other)
    by_symbol = {record.local_code: record for record in records}

    assert by_symbol["AAPL"].exchange_id == "XNAS"
    assert by_symbol["AAPL"].security_type == "COMMON_STOCK"
    assert by_symbol["TEST"].is_test_issue is True
    assert by_symbol["A"].exchange_id == "XNYS"
    assert by_symbol["SPY"].exchange_id == "ARCX"
    assert by_symbol["SPY"].security_type == "ETF"
    # 'Q' is not a valid otherlisted exchange letter: the record is kept with an
    # unresolved exchange and the problem is reported, not silently dropped.
    assert by_symbol["XYZ"].exchange_id is None
    assert any("unmapped exchange letter" in error for error in errors)


def test_file_creation_footer_is_not_a_record():
    nasdaq = "\n".join([NASDAQ_HEADER, "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N", "File Creation Time: x"])
    records, _ = parse_symbol_directory(nasdaq, OTHER_HEADER)
    assert [record.local_code for record in records] == ["AAPL"]


# ------------------------------------------- Phase 1.1b: SIC membership hashing
def test_sic_membership_hash_moves_the_run_fingerprint():
    """An empty content hash let SPAC/REIT membership change invisibly.

    _sources_data_version ignores provenances without a content hash, so a run
    whose only change was the SIC membership produced the same fingerprint - and
    therefore the same idempotency key - as the run before it.
    """

    from datetime import UTC, datetime

    from surge.jobs.universe_sync import _sources_data_version
    from surge.models import Provenance
    from surge.providers.sec_edgar import sic_membership_hash

    now = datetime.now(UTC)

    def provenance(content_hash: str) -> Provenance:
        return Provenance(
            source_id="sec_sic_directory", endpoint="https://www.sec.gov/cgi-bin/browse-edgar?SIC=6770",
            requested_at=now, received_at=now, http_status=200, bytes=1,
            content_sha256=content_hash, item_count=2, observed_at=now, available_at=now,
        )

    before = sic_membership_hash("6770", {"0000000001", "0000000002"})
    after = sic_membership_hash("6770", {"0000000001", "0000000002", "0000000003"})

    assert before != after
    assert _sources_data_version([provenance(before)]) != _sources_data_version([provenance(after)])

    # the order the SEC happens to return the pages in must not matter
    assert sic_membership_hash("6770", {"0000000002", "0000000001"}) == before
    # and it is not simply empty any more
    assert before != ""


def test_nasdaq_combined_content_hash_is_a_real_digest():
    """It was two digests joined with a colon, which is not a SHA-256."""

    import re

    from surge.providers.nasdaq_trader import combined_content_hash

    digest = combined_content_hash("a" * 64, "b" * 64)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert ":" not in digest
    # each component matters, and their order does
    assert digest != combined_content_hash("a" * 64, "c" * 64)
    assert digest != combined_content_hash("b" * 64, "a" * 64)
    assert digest == combined_content_hash("a" * 64, "b" * 64)
