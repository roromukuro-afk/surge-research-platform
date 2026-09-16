"""RF-12: universe-1.0.0 classification.

Anything the sources cannot settle must end up UNRESOLVED, never silently
excluded and never optimistically included.
"""

from __future__ import annotations

import pytest

from surge.models import RawSecurityRecord
from surge.universe import UniverseContext, classify


def record(**overrides) -> RawSecurityRecord:
    base = {
        "source_id": "fixture",
        "source_record_id": "fixture-1",
        "market_code": "US",
        "exchange_id": "XNYS",
        "local_code": "ABC",
        "symbol": "ABC",
        "name": "Example Inc. Common Stock",
        "security_type": "COMMON_STOCK",
        "currency": "USD",
        "country": "US",
        "cik": "0000000001",
    }
    base.update(overrides)
    return RawSecurityRecord(**base)


def jp_record(**overrides) -> RawSecurityRecord:
    base = {
        "source_id": "fixture",
        "source_record_id": "1301",
        "market_code": "JP",
        "exchange_id": "XTKS",
        "local_code": "13010",
        "symbol": "13010",
        "name": "サンプル株式会社",
        "security_type": "COMMON_STOCK",
        "market_segment_code": "PRIME_DOMESTIC",
        "currency": "JPY",
        "country": "JP",
    }
    base.update(overrides)
    return RawSecurityRecord(**base)


@pytest.mark.parametrize("segment", ["PRIME_DOMESTIC", "STANDARD_DOMESTIC", "GROWTH_DOMESTIC"])
def test_jp_domestic_common_stock_included(segment):
    decision = classify(jp_record(market_segment_code=segment))
    assert decision.decision == "INCLUDED"
    assert decision.reason_code == "TARGET_MARKET_COMMON_STOCK"


def test_jp_pro_market_excluded():
    decision = classify(jp_record(market_segment_code="PRO_MARKET"))
    assert (decision.decision, decision.reason_code) == ("EXCLUDED", "NOT_TARGET_SEGMENT")


def test_jp_etf_and_reit_excluded():
    etf = classify(jp_record(security_type="ETF", market_segment_code="ETF_ETN"))
    reit = classify(jp_record(security_type="REIT_OR_FUND", market_segment_code="REIT_FUND"))
    assert (etf.decision, etf.reason_code) == ("EXCLUDED", "ETF")
    assert (reit.decision, reit.reason_code) == ("EXCLUDED", "REIT_OR_FUND")


def test_jp_foreign_share_is_unresolved_not_excluded():
    decision = classify(jp_record(security_type="FOREIGN_COMMON_STOCK", market_segment_code="PRIME_FOREIGN"))
    assert decision.decision == "UNRESOLVED"
    assert decision.reason_code == "FOREIGN_STOCK_RULE_PENDING"


def test_jp_investment_certificate_is_unresolved():
    decision = classify(jp_record(security_type="INVESTMENT_CERTIFICATE", market_segment_code="INVESTMENT_CERTIFICATE"))
    assert decision.decision == "UNRESOLVED"
    assert decision.reason_code == "INVESTMENT_CERTIFICATE_RULE_PENDING"


def test_jp_unknown_category_is_unresolved():
    decision = classify(jp_record(security_type="UNKNOWN", market_segment_code="UNKNOWN"))
    assert decision.decision == "UNRESOLVED"
    assert decision.reason_code == "TYPE_UNKNOWN"


@pytest.mark.parametrize("exchange", ["XNYS", "XNAS", "XASE"])
def test_us_common_stock_on_target_exchange_included(exchange):
    decision = classify(record(exchange_id=exchange))
    assert decision.decision == "INCLUDED"


@pytest.mark.parametrize("exchange", ["ARCX", "BATS", "IEXG"])
def test_us_non_target_exchange_excluded(exchange):
    decision = classify(record(exchange_id=exchange))
    assert (decision.decision, decision.reason_code) == ("EXCLUDED", "NOT_TARGET_EXCHANGE")


def test_us_otc_excluded():
    decision = classify(record(exchange_id="OTCM"))
    assert (decision.decision, decision.reason_code) == ("EXCLUDED", "OTC")


@pytest.mark.parametrize(
    ("security_type", "reason"),
    [
        ("ETF", "ETF"),
        ("PREFERRED", "PREFERRED"),
        ("WARRANT", "WARRANT"),
        ("RIGHT", "RIGHT"),
        ("UNIT", "UNIT"),
        ("OTHER", "NOT_COMMON_STOCK"),
    ],
)
def test_us_non_common_types_excluded(security_type, reason):
    decision = classify(record(security_type=security_type))
    assert (decision.decision, decision.reason_code) == ("EXCLUDED", reason)


def test_us_test_issue_excluded():
    decision = classify(record(is_test_issue=True))
    assert (decision.decision, decision.reason_code) == ("EXCLUDED", "TEST_ISSUE")


def test_us_pre_merger_spac_excluded_by_sic():
    context = UniverseContext(spac_ciks=frozenset({"0000000001"}))
    decision = classify(record(), context)
    assert (decision.decision, decision.reason_code) == ("EXCLUDED", "SPAC_PRE_MERGER")


def test_us_reit_excluded_by_sic():
    context = UniverseContext(reit_ciks=frozenset({"0000000001"}))
    decision = classify(record(), context)
    assert (decision.decision, decision.reason_code) == ("EXCLUDED", "REIT_OR_FUND")


def test_us_adr_is_unresolved_until_eligibility_is_defined():
    decision = classify(record(security_type="ADR", is_adr=True))
    assert decision.decision == "UNRESOLVED"
    assert decision.reason_code == "ADR_ELIGIBILITY_UNDEFINED"


def test_us_unknown_type_is_unresolved():
    decision = classify(record(security_type="UNKNOWN"))
    assert decision.decision == "UNRESOLVED"
    assert decision.reason_code == "TYPE_UNKNOWN"


def test_us_common_stock_without_cik_is_unresolved_not_included():
    decision = classify(record(cik=None))
    assert decision.decision == "UNRESOLVED"
    assert decision.reason_code == "REIT_DETECTION_INCOMPLETE"


def test_us_missing_exchange_is_unresolved():
    decision = classify(record(exchange_id=None))
    assert decision.decision == "UNRESOLVED"
    assert decision.reason_code == "PROVIDER_DATA_MISSING"


def test_delisted_is_excluded_in_both_markets():
    assert classify(record(listing_status="DELISTED")).reason_code == "DELISTED"
    assert classify(jp_record(listing_status="DELISTED")).reason_code == "DELISTED"
