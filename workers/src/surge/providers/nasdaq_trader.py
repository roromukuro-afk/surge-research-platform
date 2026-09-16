"""US security master from the Nasdaq Trader Symbol Directory.

nasdaqlisted.txt covers Nasdaq; otherlisted.txt covers the other listing venues
and carries the exchange code, the ETF flag and the test issue flag.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from surge.http_fetch import fetch
from surge.models import FetchResult, Provenance, ProviderCapabilities, RawSecurityRecord
from surge.normalize import normalize_symbol
from surge.providers.base import LATENCY_DAILY_BATCH, ROLE_SECURITY_MASTER

SOURCE_ID = "nasdaq_trader_symbol_directory"
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Exchange letters used by otherlisted.txt.
EXCHANGE_MAP = {
    "A": "XASE",  # NYSE American
    "N": "XNYS",  # NYSE
    "P": "ARCX",  # NYSE Arca
    "Z": "BATS",  # Cboe BZX
    "V": "IEXG",  # IEX
}

_ADR_PATTERNS = (
    "american depositary",
    "american depository",
    "depositary receipt",
    "depository receipt",
    "new york registry shares",
)

# "ADS" / "ADR" as standalone tokens. The lookarounds keep company names such as
# "ADS-TEC Energy" from being read as depositary receipts.
_ADS_TOKEN = re.compile(r"(?<![\w-])ad[rs]s?(?![\w-])")

_COMMON_PATTERNS = (
    re.compile(r"\bcommon stocks?\b"),
    re.compile(r"\bcommon shares?\b"),
    re.compile(r"\bordinary shares?\b"),
    re.compile(r"\bclass [a-z] ordinary\b"),
    re.compile(r"\bcommon units?\b"),
)

_FUND_PATTERNS = (
    re.compile(r"\bclosed[- ]end fund\b"),
    re.compile(r"\breal estate investment trust\b"),
)


def classify_security_name(name: str, *, etf_flag: str | None) -> tuple[str, bool, dict[str, str]]:
    """Derive security type from the published security name and ETF flag.

    Returns ``(security_type, is_adr, evidence)``. Anything the naming does not
    clearly identify stays UNKNOWN so it can be reported as unresolved.
    """

    # The security name itself is stored in its own column; evidence keeps only
    # the flags that drove the decision.
    lowered = f" {name.lower()} "
    evidence: dict[str, str] = {}
    if etf_flag:
        evidence["etf_flag"] = etf_flag

    if etf_flag == "Y":
        return "ETF", False, evidence

    is_adr = any(pattern in lowered for pattern in _ADR_PATTERNS) or bool(_ADS_TOKEN.search(lowered))

    if any(pattern.search(lowered) for pattern in _FUND_PATTERNS):
        return "REIT_OR_FUND", is_adr, evidence
    if "warrant" in lowered:
        return "WARRANT", is_adr, evidence
    if re.search(r"\brights?\b", lowered):
        return "RIGHT", is_adr, evidence
    if re.search(r"\bunits?\b", lowered):
        return "UNIT", is_adr, evidence
    # Preferred is checked before ADR so that a preferred ADS is classified as
    # preferred, but "American Depositary Shares" on its own stays an ADR.
    if "preferred" in lowered or re.search(r"\bpfd\b", lowered):
        return "PREFERRED", is_adr, evidence
    if is_adr:
        return "ADR", True, evidence
    if "depositary shares" in lowered:
        return "PREFERRED", is_adr, evidence
    if "exchange traded note" in lowered or re.search(r"\betns?\b", lowered):
        return "ETN", is_adr, evidence
    if any(token in lowered for token in ("notes due", "debenture", "subordinated note", "senior note")):
        return "OTHER", is_adr, evidence
    if any(pattern.search(lowered) for pattern in _COMMON_PATTERNS):
        return "COMMON_STOCK", False, evidence

    return "UNKNOWN", is_adr, evidence


class NasdaqTraderProvider:
    """Reads both symbol directory files. No credentials required."""

    provider_id = "nasdaq_trader_symbol_directory"

    def __init__(
        self,
        nasdaq_url: str = NASDAQ_URL,
        other_url: str = OTHER_URL,
        *,
        user_agent: str | None = None,
    ) -> None:
        self._nasdaq_url = nasdaq_url
        self._other_url = other_url
        self._user_agent = user_agent

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            markets=("US",),
            roles=(ROLE_SECURITY_MASTER,),
            update_schedule="daily file publication",
            latency_class=LATENCY_DAILY_BATCH,
            license_scope="public exchange publication",
            provides_delisted=False,
        )

    def fetch_security_master(self) -> FetchResult:
        kwargs = {"user_agent": self._user_agent} if self._user_agent else {}
        nasdaq = fetch(self._nasdaq_url, **kwargs)
        other = fetch(self._other_url, **kwargs)

        records, errors = parse_symbol_directory(
            nasdaq.body.decode("utf-8", errors="replace"),
            other.body.decode("utf-8", errors="replace"),
        )

        provenance = Provenance(
            source_id=SOURCE_ID,
            endpoint=f"{self._nasdaq_url} + {self._other_url}",
            requested_at=nasdaq.requested_at,
            received_at=other.received_at,
            http_status=max(nasdaq.status, other.status),
            bytes=nasdaq.bytes + other.bytes,
            content_sha256=f"{nasdaq.sha256}:{other.sha256}",
            item_count=len(records),
            observed_at=other.received_at,
            available_at=other.received_at,
        )
        return FetchResult(records=tuple(records), provenance=provenance, errors=tuple(errors))


def _rows(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [column.strip() for column in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        values = [value.strip() for value in line.split("|")]
        if len(values) != len(header):
            continue
        rows.append(dict(zip(header, values, strict=True)))
    return rows


def parse_symbol_directory(
    nasdaq_text: str,
    other_text: str,
    *,
    observed_at: datetime | None = None,
) -> tuple[list[RawSecurityRecord], list[str]]:
    observed_at = observed_at or datetime.now(UTC)
    records: list[RawSecurityRecord] = []
    errors: list[str] = []

    for row in _rows(nasdaq_text):
        symbol = normalize_symbol(row.get("Symbol", ""))
        if not symbol:
            continue
        name = row.get("Security Name", "")
        security_type, is_adr, evidence = classify_security_name(name, etf_flag=row.get("ETF"))
        evidence["market_category"] = row.get("Market Category", "")
        evidence["financial_status"] = row.get("Financial Status", "")
        records.append(
            RawSecurityRecord(
                source_id=SOURCE_ID,
                source_record_id=f"nasdaqlisted:{symbol}",
                market_code="US",
                exchange_id="XNAS",
                local_code=symbol,
                symbol=symbol,
                name=name,
                security_type=security_type,
                market_segment_code=row.get("Market Category") or None,
                market_segment_name=None,
                currency="USD",
                country="US",
                is_adr=is_adr,
                is_test_issue=row.get("Test Issue") == "Y",
                listing_status="LISTED",
                type_evidence=evidence,
            )
        )

    for row in _rows(other_text):
        symbol = normalize_symbol(row.get("ACT Symbol", ""))
        if not symbol:
            continue
        exchange_letter = row.get("Exchange", "")
        exchange_id = EXCHANGE_MAP.get(exchange_letter)
        if exchange_id is None:
            errors.append(f"unmapped exchange letter for {symbol}: {exchange_letter!r}")
        name = row.get("Security Name", "")
        security_type, is_adr, evidence = classify_security_name(name, etf_flag=row.get("ETF"))
        evidence["exchange_letter"] = exchange_letter
        records.append(
            RawSecurityRecord(
                source_id=SOURCE_ID,
                source_record_id=f"otherlisted:{symbol}",
                market_code="US",
                exchange_id=exchange_id,
                local_code=symbol,
                symbol=symbol,
                name=name,
                security_type=security_type,
                market_segment_code=exchange_letter or None,
                market_segment_name=None,
                currency="USD",
                country="US",
                is_adr=is_adr,
                is_test_issue=row.get("Test Issue") == "Y",
                listing_status="LISTED",
                type_evidence=evidence,
            )
        )

    return records, errors
