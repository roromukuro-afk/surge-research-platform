"""SEC EDGAR helpers: CIK mapping and SIC based classification support.

SEC asks for a declared User-Agent and at most 10 requests per second; both are
honoured here. Only identifiers and classification flags are read - no filing
text is stored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from surge.http_fetch import fetch
from surge.models import Provenance

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SIC_DIRECTORY_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

SIC_BLANK_CHECK = "6770"
SIC_REIT = "6798"

_CIK_RE = re.compile(r"<cik>(\d+)</cik>", re.IGNORECASE)

# SEC allows 10 requests/second; stay well under it.
_MIN_INTERVAL = 0.2


@dataclass(frozen=True)
class CikRecord:
    cik: str
    name: str
    exchange: str | None


@dataclass(frozen=True)
class SicMembership:
    sic: str
    ciks: frozenset[str]
    pages_fetched: int
    truncated: bool
    provenance: Provenance


def _pad_cik(value: int | str) -> str:
    return str(int(value)).zfill(10)


class SecCompanyTickers:
    """Maps tickers to CIK and the exchange SEC believes the ticker trades on."""

    provider_id = "sec_company_tickers"

    def __init__(self, url: str = COMPANY_TICKERS_URL, *, user_agent: str | None = None) -> None:
        self._url = url
        self._user_agent = user_agent

    def fetch_map(self) -> tuple[dict[str, CikRecord], Provenance]:
        kwargs = {"user_agent": self._user_agent} if self._user_agent else {}
        response = fetch(self._url, min_interval=_MIN_INTERVAL, **kwargs)
        payload = json.loads(response.body.decode("utf-8"))
        mapping = parse_company_tickers(payload)

        provenance = Provenance(
            source_id="sec_company_tickers",
            endpoint=self._url,
            requested_at=response.requested_at,
            received_at=response.received_at,
            http_status=response.status,
            bytes=response.bytes,
            content_sha256=response.sha256,
            item_count=len(mapping),
            observed_at=response.received_at,
            available_at=response.received_at,
        )
        return mapping, provenance


def parse_company_tickers(payload: dict) -> dict[str, CikRecord]:
    fields = [str(field).lower() for field in payload.get("fields", [])]
    rows = payload.get("data", [])
    mapping: dict[str, CikRecord] = {}
    for row in rows:
        record = dict(zip(fields, row, strict=False))
        ticker = str(record.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        mapping[ticker] = CikRecord(
            cik=_pad_cik(record.get("cik", 0)),
            name=str(record.get("name", "")),
            exchange=(str(record["exchange"]) if record.get("exchange") else None),
        )
    return mapping


class SecSicDirectory:
    """Lists the CIKs registered under an SIC code (e.g. blank checks, REITs)."""

    provider_id = "sec_sic_directory"

    def __init__(self, url: str = SIC_DIRECTORY_URL, *, user_agent: str | None = None) -> None:
        self._url = url
        self._user_agent = user_agent

    def fetch_membership(self, sic: str, *, max_pages: int = 60, page_size: int = 100) -> SicMembership:
        kwargs = {"user_agent": self._user_agent} if self._user_agent else {}
        ciks: set[str] = set()
        pages = 0
        truncated = False
        first_requested: datetime | None = None
        last_received: datetime | None = None
        status = 0
        total_bytes = 0

        for page in range(max_pages):
            start = page * page_size
            url = (
                f"{self._url}?action=getcompany&SIC={sic}&owner=include"
                f"&count={page_size}&start={start}&output=atom"
            )
            response = fetch(url, min_interval=_MIN_INTERVAL, **kwargs)
            first_requested = first_requested or response.requested_at
            last_received = response.received_at
            status = response.status
            total_bytes += response.bytes
            pages += 1

            found = {_pad_cik(value) for value in _CIK_RE.findall(response.body.decode("utf-8", errors="replace"))}
            new = found - ciks
            ciks |= found
            if len(found) < page_size or not new:
                break
        else:
            truncated = True

        assert first_requested is not None and last_received is not None
        provenance = Provenance(
            source_id="sec_sic_directory",
            endpoint=f"{self._url}?SIC={sic}",
            requested_at=first_requested,
            received_at=last_received,
            http_status=status,
            bytes=total_bytes,
            content_sha256="",
            item_count=len(ciks),
            observed_at=last_received,
            available_at=last_received,
        )
        return SicMembership(
            sic=sic,
            ciks=frozenset(ciks),
            pages_fetched=pages,
            truncated=truncated,
            provenance=provenance,
        )
