"""universe-1.0.0.

Implements docs/specs/universe-definition-v1.0.0.md.

Two rules shape everything here:

* nothing is included unless the sources positively say it belongs;
* nothing is excluded just because the sources were unclear - that case is
  UNRESOLVED, so it stays visible in coverage instead of silently vanishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from surge.models import RawSecurityRecord, UniverseDecision

UNIVERSE_VERSION = "universe-1.0.0"
SPEC_PATH = "docs/specs/universe-definition-v1.0.0.md"

JP_TARGET_SEGMENTS = frozenset({"PRIME_DOMESTIC", "STANDARD_DOMESTIC", "GROWTH_DOMESTIC"})
JP_FOREIGN_SEGMENTS = frozenset({"PRIME_FOREIGN", "STANDARD_FOREIGN", "GROWTH_FOREIGN"})
JP_EXCLUDED_SEGMENTS = frozenset({"PRO_MARKET"})

US_TARGET_EXCHANGES = frozenset({"XNYS", "XNAS", "XASE"})
US_NON_TARGET_EXCHANGES = frozenset({"ARCX", "BATS", "IEXG"})

_TYPE_EXCLUSIONS = {
    "ETF": "ETF",
    "ETN": "ETN",
    "REIT_OR_FUND": "REIT_OR_FUND",
    "PREFERRED": "PREFERRED",
    "WARRANT": "WARRANT",
    "RIGHT": "RIGHT",
    "UNIT": "UNIT",
    "OTHER": "NOT_COMMON_STOCK",
}


@dataclass(frozen=True)
class UniverseContext:
    """External facts the definition needs beyond the provider record itself."""

    spac_ciks: frozenset[str] = field(default_factory=frozenset)
    reit_ciks: frozenset[str] = field(default_factory=frozenset)
    sic_lookup_available: bool = True


def classify(record: RawSecurityRecord, context: UniverseContext | None = None) -> UniverseDecision:
    context = context or UniverseContext()

    if record.is_test_issue:
        return UniverseDecision("EXCLUDED", "TEST_ISSUE", {"test_issue": True})

    if record.listing_status == "DELISTED":
        return UniverseDecision("EXCLUDED", "DELISTED", {"listing_status": record.listing_status})

    if record.market_code == "JP":
        return _classify_jp(record)
    if record.market_code == "US":
        return _classify_us(record, context)

    return UniverseDecision("UNRESOLVED", "PROVIDER_DATA_MISSING", {"market_code": record.market_code})


def _classify_jp(record: RawSecurityRecord) -> UniverseDecision:
    segment = record.market_segment_code
    detail = {"segment": segment, "security_type": record.security_type}

    if segment in JP_EXCLUDED_SEGMENTS:
        return UniverseDecision("EXCLUDED", "NOT_TARGET_SEGMENT", detail)

    if record.security_type in ("ETF", "ETN"):
        return UniverseDecision("EXCLUDED", record.security_type, detail)
    if record.security_type == "REIT_OR_FUND":
        return UniverseDecision("EXCLUDED", "REIT_OR_FUND", detail)
    if record.security_type == "INVESTMENT_CERTIFICATE":
        return UniverseDecision("UNRESOLVED", "INVESTMENT_CERTIFICATE_RULE_PENDING", detail)
    if record.security_type == "FOREIGN_COMMON_STOCK" or segment in JP_FOREIGN_SEGMENTS:
        return UniverseDecision("UNRESOLVED", "FOREIGN_STOCK_RULE_PENDING", detail)
    if record.security_type == "UNKNOWN" or segment == "UNKNOWN":
        return UniverseDecision("UNRESOLVED", "TYPE_UNKNOWN", detail)

    if segment in JP_TARGET_SEGMENTS and record.security_type == "COMMON_STOCK":
        return UniverseDecision("INCLUDED", "TARGET_MARKET_COMMON_STOCK", detail)

    if segment not in JP_TARGET_SEGMENTS:
        return UniverseDecision("UNRESOLVED", "SEGMENT_RULE_PENDING", detail)

    return UniverseDecision("EXCLUDED", "NOT_COMMON_STOCK", detail)


def _classify_us(record: RawSecurityRecord, context: UniverseContext) -> UniverseDecision:
    detail = {
        "exchange_id": record.exchange_id,
        "security_type": record.security_type,
        "cik": record.cik,
    }

    if record.exchange_id is None:
        return UniverseDecision("UNRESOLVED", "PROVIDER_DATA_MISSING", detail)
    if record.exchange_id == "OTCM":
        return UniverseDecision("EXCLUDED", "OTC", detail)
    if record.exchange_id in US_NON_TARGET_EXCHANGES:
        return UniverseDecision("EXCLUDED", "NOT_TARGET_EXCHANGE", detail)
    if record.exchange_id not in US_TARGET_EXCHANGES:
        return UniverseDecision("EXCLUDED", "NOT_TARGET_EXCHANGE", detail)

    excluded_reason = _TYPE_EXCLUSIONS.get(record.security_type)
    if excluded_reason:
        return UniverseDecision("EXCLUDED", excluded_reason, detail)

    if record.cik and record.cik in context.spac_ciks:
        return UniverseDecision("EXCLUDED", "SPAC_PRE_MERGER", {**detail, "sic": "6770"})
    if record.cik and record.cik in context.reit_ciks:
        return UniverseDecision("EXCLUDED", "REIT_OR_FUND", {**detail, "sic": "6798"})

    if record.security_type == "ADR" or record.is_adr:
        # Eligibility rule for ADRs is not decided yet (D-10a): stay visible.
        return UniverseDecision("UNRESOLVED", "ADR_ELIGIBILITY_UNDEFINED", detail)

    if record.security_type == "UNKNOWN":
        return UniverseDecision("UNRESOLVED", "TYPE_UNKNOWN", detail)

    if record.security_type == "COMMON_STOCK":
        if not context.sic_lookup_available:
            return UniverseDecision(
                "UNRESOLVED", "REIT_DETECTION_INCOMPLETE", {**detail, "reason": "sic_lookup_unavailable"}
            )
        if not record.cik:
            # Without a CIK the REIT / blank check checks cannot be run.
            return UniverseDecision("UNRESOLVED", "REIT_DETECTION_INCOMPLETE", {**detail, "reason": "cik_unknown"})
        return UniverseDecision("INCLUDED", "TARGET_MARKET_COMMON_STOCK", detail)

    return UniverseDecision("EXCLUDED", "NOT_COMMON_STOCK", detail)
