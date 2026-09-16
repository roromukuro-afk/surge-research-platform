"""Identity fixtures A-D from the Phase 1.1 audit.

A ticker is an attribute. A normalised name is evidence. Neither may decide who
an issuer is or which security a listing points at.
"""

from __future__ import annotations

from surge.identity import (
    DEMOTABLE,
    PROVISIONAL,
    REGISTRY_ANCHORED,
    STRONG,
    demote_colliding_identities,
    issuer_identity,
    security_identity,
)
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
    issuer = issuer_identity(
        market_code="JP", security_identity_key=security.key, edinet_code="E00012"
    )

    assert security.key == "JP:JPX:1301"
    assert security.confidence == STRONG
    assert issuer.key == "EDINET:E00012"
    assert issuer.confidence == STRONG


def test_jp_issuer_without_edinet_is_provisional_per_security():
    issuer = issuer_identity(market_code="JP", security_identity_key="JP:JPX:1301")
    assert issuer.confidence == PROVISIONAL
    assert issuer.source == "PROVISIONAL_SECURITY_COORDINATE"
    assert issuer.key == "ISSUER-OF:JP:JPX:1301"


# ------------------------------------------------- Phase 1.1a: no name merges
def test_identical_names_without_a_registry_id_stay_separate_issuers():
    """A name is evidence, not an identity: a false split beats a false merge."""

    first = us_record(symbol="AAA", local_code="AAA", cik=None, name="Acme Holdings Inc.")
    second = us_record(
        source_record_id="r2", symbol="BBB", local_code="BBB", cik=None, name="Acme Holdings Inc."
    )

    issuers, securities, _ = resolve_identities([first, second])

    assert normalize_name(first.name) == normalize_name(second.name), "the names really do collide"
    assert issuers[0].key != issuers[1].key
    assert all(identity.confidence == PROVISIONAL for identity in issuers)
    # the fallback follows the security coordinate, so it is stable across runs
    assert issuers[0].key == f"ISSUER-OF:{securities[0].key}"


def test_registry_identifier_still_merges_the_issuer():
    """The no-merge rule applies only where there is no registry identifier."""

    common = us_record(symbol="ABC", local_code="ABC")
    preferred = us_record(
        source_record_id="r2", symbol="ABCP", local_code="ABCP",
        name="Example Inc. 6% Series A Preferred", security_type="PREFERRED",
    )

    issuers, _, _ = resolve_identities([common, preferred])

    assert issuers[0].key == issuers[1].key == "CIK:0000000001"


# --------------------------------------- Phase 1.1a: honest US confidence tier
def test_us_cik_security_identity_is_registry_anchored_not_strong():
    """The key carries a type and a class token read out of a display name."""

    identity = security_identity(
        market_code="US", exchange_id="XNAS", local_code="ABC", symbol="ABC",
        name="Example Inc. Class A Common Stock", security_type="COMMON_STOCK", cik="0000000001",
    )

    assert identity.confidence == REGISTRY_ANCHORED
    assert identity.confidence != STRONG


def test_provider_name_formatting_change_never_claims_a_stable_security():
    """Renaming the product moves the key, so the key must not claim stability."""

    before = security_identity(
        market_code="US", exchange_id="XNAS", local_code="ABC", symbol="ABC",
        name="Example Inc. Common Stock", security_type="COMMON_STOCK", cik="0000000001",
    )
    after = security_identity(
        market_code="US", exchange_id="XNAS", local_code="ABC", symbol="ABC",
        name="Example Inc. Class A Common Stock", security_type="COMMON_STOCK", cik="0000000001",
    )

    assert before.key != after.key, "formatting really does move the key"
    assert {before.confidence, after.confidence} == {REGISTRY_ANCHORED}
    # A JP security is keyed on a registry code, so a rename cannot move it.
    jp_before = security_identity(
        market_code="JP", exchange_id="XTKS", local_code="1301", symbol="1301",
        name="極洋", security_type="COMMON_STOCK",
    )
    jp_after = security_identity(
        market_code="JP", exchange_id="XTKS", local_code="1301", symbol="1301",
        name="株式会社極洋", security_type="COMMON_STOCK",
    )
    assert jp_before.key == jp_after.key
    assert jp_before.confidence == STRONG


def test_registry_anchored_collisions_are_demoted_like_strong_ones():
    """A new confidence tier must not become invisible to collision detection."""

    # Two leveraged notes of one issuer: no class or series wording anywhere, so
    # the CIK + type + class key cannot tell them apart. This is the real shape
    # of the 67 colliding keys in the US master.
    first = us_record(symbol="AAAU", local_code="AAAU", name="Example 3X Long ETNs due 2030", security_type="ETN")
    second = us_record(
        source_record_id="r2", symbol="AAAD", local_code="AAAD",
        name="Example 3X Inverse ETNs due 2030", security_type="ETN",
    )

    _, securities, collisions = resolve_identities([first, second])

    assert REGISTRY_ANCHORED in DEMOTABLE
    assert securities[0].key != securities[1].key
    assert all(identity.confidence == PROVISIONAL for identity in securities)
    assert len(collisions) == 2
    assert {collision.identity_key for collision in collisions} == {"US:CIK:0000000001:ETN:"}


def test_collision_report_carries_a_queryable_context():
    first = us_record(symbol="WTA", local_code="WTA", name="Example Inc. Warrant", security_type="WARRANT")
    second = us_record(
        source_record_id="r2", symbol="WTB", local_code="WTB",
        name="Example Inc. Warrant", security_type="WARRANT",
    )

    _, _, collisions = resolve_identities([first, second])

    assert [collision.context["symbol"] for collision in collisions] == ["WTA", "WTB"]
    assert {collision.context["identity_key"] for collision in collisions} == {
        "US:CIK:0000000001:WARRANT:"
    }
    assert all(collision.context["exchange_id"] == "XNAS" for collision in collisions)


def test_demote_is_not_gated_on_a_single_confidence_value():
    """Guards the exact defect: `== STRONG` would silently merge the new tier."""

    from surge.identity import Identity

    colliding = [
        Identity("SEC_CIK", "US:CIK:1:ETF:", REGISTRY_ANCHORED),
        Identity("SEC_CIK", "US:CIK:1:ETF:", REGISTRY_ANCHORED),
    ]
    resolved, collisions = demote_colliding_identities(
        colliding,
        exchange_ids=["XNAS", "XNAS"],
        symbols=["ONE", "TWO"],
        local_codes=["ONE", "TWO"],
        market_codes=["US", "US"],
    )

    assert resolved[0].key != resolved[1].key
    assert len(collisions) == 2
