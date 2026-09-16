"""Identity fixtures A-D from the Phase 1.1 audit.

A ticker is an attribute. A normalised name is evidence. Neither may decide who
an issuer is or which security a listing points at.
"""

from __future__ import annotations

from surge.identity import PROVISIONAL, STRONG, issuer_identity, security_identity
from surge.jobs.universe_sync import resolve_identities
from surge.models import RawSecurityRecord
from surge.normalize import normalize_name


def us_record(**overrides) -> RawSecurityRecord:
    base = {
        "source_id": "fixture",
        "source_record_id": "r1",
        "market_code": "US",
        "exchange_id": "XNAS",
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


# ------------------------------------------------------------------ fixture A
def test_same_cik_multiple_securities_share_one_issuer():
    common = us_record(symbol="ABC", local_code="ABC", security_type="COMMON_STOCK")
    preferred = us_record(
        source_record_id="r2", symbol="ABCP", local_code="ABCP",
        name="Example Inc. 6% Series A Preferred", security_type="PREFERRED",
    )

    issuers, securities, notes = resolve_identities([common, preferred])

    assert issuers[0].key == issuers[1].key == "CIK:0000000001"
    assert issuers[0].confidence == STRONG
    # ... but they are different instruments
    assert securities[0].key != securities[1].key
    assert not notes


# ------------------------------------------------------------------ fixture B
def test_different_ciks_with_colliding_names_stay_different_issuers():
    a = us_record(cik="0000000111", name="Independent Bank Corp.")
    b = us_record(source_record_id="r2", symbol="IBCP", local_code="IBCP",
                  cik="0000000222", name="Independent Bank Corporation")

    issuers, _, _ = resolve_identities([a, b])

    assert normalize_name(a.name) == normalize_name(b.name), "the names really do collide"
    assert issuers[0].key != issuers[1].key


# ------------------------------------------------------------------ fixture C
def test_ticker_change_keeps_security_and_listing_identity():
    before = us_record(symbol="ABC", local_code="ABC")
    after = us_record(symbol="XYZ", local_code="XYZ")

    _, securities_before, _ = resolve_identities([before])
    _, securities_after, _ = resolve_identities([after])

    assert securities_before[0].key == securities_after[0].key
    assert "ABC" not in securities_before[0].key and "XYZ" not in securities_after[0].key


# ------------------------------------------------------------------ fixture D
def test_name_change_only_keeps_issuer_and_security_identity():
    before = us_record(name="Example Inc. Common Stock")
    after = us_record(name="Example Holdings Inc. Common Stock")

    issuers_before, securities_before, _ = resolve_identities([before])
    issuers_after, securities_after, _ = resolve_identities([after])

    assert issuers_before[0].key == issuers_after[0].key
    assert securities_before[0].key == securities_after[0].key


def test_share_classes_of_one_issuer_are_different_securities():
    class_a = us_record(symbol="EXA", local_code="EXA", name="Example Inc. Class A Common Stock")
    class_b = us_record(source_record_id="r2", symbol="EXB", local_code="EXB",
                        name="Example Inc. Class B Common Stock")

    _, securities, notes = resolve_identities([class_a, class_b])

    assert securities[0].key != securities[1].key
    assert not notes


def test_identity_collision_falls_back_to_provisional_and_is_reported():
    """Two indistinguishable instruments must not be merged into one identity."""

    first = us_record(symbol="WTA", local_code="WTA", name="Example Inc. Warrant", security_type="WARRANT")
    second = us_record(source_record_id="r2", symbol="WTB", local_code="WTB",
                       name="Example Inc. Warrant", security_type="WARRANT")

    _, securities, notes = resolve_identities([first, second])

    assert securities[0].key != securities[1].key
    assert all(identity.confidence == PROVISIONAL for identity in securities)
    assert len(notes) == 2


def test_us_security_without_cik_is_provisional():
    identity = security_identity(
        market_code="US", exchange_id="XNAS", local_code="NOCIK", symbol="NOCIK",
        name="Unknown Corp Common Stock", security_type="COMMON_STOCK", cik=None,
    )
    assert identity.confidence == PROVISIONAL
    assert identity.source == "PROVISIONAL_EXCHANGE_SYMBOL"


def test_jp_identity_uses_exchange_code_and_edinet():
    security = security_identity(
        market_code="JP", exchange_id="XTKS", local_code="1301", symbol="1301",
        name="極洋", security_type="COMMON_STOCK",
    )
    issuer = issuer_identity(market_code="JP", normalized_name="極洋", edinet_code="E00012")

    assert security.key == "JP:JPX:1301"
    assert security.confidence == STRONG
    assert issuer.key == "EDINET:E00012"
    assert issuer.confidence == STRONG


def test_jp_issuer_without_edinet_is_provisional():
    issuer = issuer_identity(market_code="JP", normalized_name="名もなき会社")
    assert issuer.confidence == PROVISIONAL
    assert issuer.source == "PROVISIONAL_NAME"
