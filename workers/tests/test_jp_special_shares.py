"""Phase 1.1b: the JPX market column says where a line trades, not what it is.

universe-1.0.0 includes domestic COMMON stock on Prime / Standard / Growth.
Preferred shares and bond-type class shares are listed on those same segments,
so classifying by segment alone put seven of them in the investable universe.

The seven rows below are the real ones from the official workbook of
2026-09-16, with their real codes and names.
"""

from __future__ import annotations

import pytest

from surge.models import RawSecurityRecord
from surge.providers.jpx_listed import classify_special_share
from surge.universe import classify

# code, name, expected security type, expected decision, expected reason
REAL_SPECIAL_SHARES = [
    ("25935", "伊藤園第１種優先株式", "PREFERRED", "EXCLUDED", "PREFERRED"),
    ("50765", "インフロニア・ホールディングス第１回社債型種類株式", "OTHER", "EXCLUDED", "NOT_COMMON_STOCK"),
    ("75505", "ゼンショーホールディングス第１回社債型種類株式", "OTHER", "EXCLUDED", "NOT_COMMON_STOCK"),
    ("92015", "日本航空株式会社第１回社債型種類株式", "OTHER", "EXCLUDED", "NOT_COMMON_STOCK"),
    ("92025", "ＡＮＡホールディングス第１回社債型種類株式", "OTHER", "EXCLUDED", "NOT_COMMON_STOCK"),
    ("94345", "ソフトバンク第１回社債型種類株式", "OTHER", "EXCLUDED", "NOT_COMMON_STOCK"),
    ("94346", "ソフトバンク第２回社債型種類株式", "OTHER", "EXCLUDED", "NOT_COMMON_STOCK"),
]


def jp_record(code: str, name: str, security_type: str, segment: str = "PRIME_DOMESTIC") -> RawSecurityRecord:
    return RawSecurityRecord(
        source_id="jpx_listed_issues",
        source_record_id=code,
        market_code="JP",
        exchange_id="XTKS",
        local_code=code,
        symbol=code,
        name=name,
        security_type=security_type,
        market_segment_code=segment,
        market_segment_name="プライム（内国株式）",
        currency="JPY",
        country="JP",
    )


@pytest.mark.parametrize("code,name,expected_type,expected_decision,expected_reason", REAL_SPECIAL_SHARES)
def test_real_special_shares_are_not_common_stock(code, name, expected_type, expected_decision, expected_reason):
    detected = classify_special_share(code, name)
    assert detected is not None, f"{code} {name} was treated as ordinary common stock"
    assert detected[0] == expected_type

    decision = classify(jp_record(code, name, detected[0]))
    assert (decision.decision, decision.reason_code) == (expected_decision, expected_reason)


def test_ordinary_shares_are_untouched():
    """The control: the other 4,434 rows must keep their classification."""

    for code, name in [("1301", "極洋"), ("7203", "トヨタ自動車"), ("9984", "ソフトバンクグループ")]:
        assert classify_special_share(code, name) is None
        decision = classify(jp_record(code, name, "COMMON_STOCK"))
        assert (decision.decision, decision.reason_code) == ("INCLUDED", "TARGET_MARKET_COMMON_STOCK")


def test_the_five_character_form_of_an_ordinary_code_is_still_ordinary():
    """13010 is 1301 written in the five character form, not a class share."""

    assert classify_special_share("13010", "極洋") is None


def test_an_unknown_special_code_is_not_guessed_into_the_universe():
    """A future instrument we have no wording for must not become common stock."""

    detected = classify_special_share("12345", "何らかの新しい証券")
    assert detected is not None
    assert detected[0] == "UNKNOWN"

    decision = classify(jp_record("12345", "何らかの新しい証券", "UNKNOWN"))
    assert (decision.decision, decision.reason_code) == ("UNRESOLVED", "TYPE_UNKNOWN")


def test_future_wording_of_the_same_kind_is_caught():
    """The rule is the instrument wording, not this particular list of seven."""

    for name in [
        "架空ホールディングス第３回社債型種類株式",
        "架空製作所第２種優先株式",
        "架空銀行優先出資証券",
    ]:
        assert classify_special_share("99999", name) is not None
        assert classify_special_share("9999", name) is not None, "four character codes must be checked too"


def test_preferred_and_class_shares_are_distinguished():
    assert classify_special_share("25935", "伊藤園第１種優先株式")[0] == "PREFERRED"
    assert classify_special_share("94345", "ソフトバンク第１回社債型種類株式")[0] == "OTHER"
