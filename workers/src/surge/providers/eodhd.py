"""EODHD: the whole US market in one request, with one trap in it.

The trap is the volume column. EODHD documents that open, high, low and close
are raw - adjusted for neither splits nor dividends - that ``adjusted_close`` is
adjusted for both, and that **volume is adjusted for splits**. A schema that
called all five "raw" would be wrong about exactly one of them, and wrong in a
way that only shows up as a quietly incorrect turnover years later. So every
column here declares its own basis, and the raw share count, if we ever need it,
is reconstructed into a separate table rather than written over the vendor's
number.

Two more documented behaviours the adapter encodes rather than comments on:

* ``delisted=1`` REPLACES the symbol list with delisted names. It does not
  extend it. A job that wants both must make both calls.
* A bulk request costs 100 API calls against the daily quota even though it is
  one HTTP request. The quota model below counts what the provider counts, not
  what the transport does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from surge.http_fetch import fetch
from surge.models import Provenance

EODHD_BASE = "https://eodhd.com/api"
PROVIDER_ID = "eodhd"

BULK_DATASET = "EODHD_US_EOD_BULK"
SPLITS_DATASET = "EODHD_SPLITS"
DIVIDENDS_DATASET = "EODHD_DIVIDENDS"
SYMBOL_LIST_DATASET = "EODHD_SYMBOL_LIST"
SYMBOL_CHANGES_DATASET = "EODHD_SYMBOL_CHANGES"
FX_DATASET = "EODHD_FX_EOD"

# Documented adjustment bases. These are the whole reason this adapter exists in
# the shape it does; they are constants so a reader cannot miss them.
OHLC_BASIS = "RAW"
VOLUME_BASIS = "SPLIT_ADJUSTED"
ADJUSTED_CLOSE_BASIS = "SPLIT_AND_DIVIDEND_ADJUSTED"

# Documented quota accounting: calls, not HTTP requests.
BULK_EXCHANGE_CALL_COST = 100
SINGLE_CALL_COST = 1


class EodhdFieldError(RuntimeError):
    """A documented field was missing. Surfaced, never silently defaulted."""


@dataclass
class QuotaLedger:
    """What this run has spent against the documented daily allowance.

    EODHD's paid allowance is 100,000 calls a day and one bulk request costs
    100 of them. Counting HTTP requests would make a backfill look a hundred
    times cheaper than it is.
    """

    calls: int = 0
    requests: int = 0
    by_dataset: dict[str, int] = field(default_factory=dict)

    def charge(self, dataset_key: str, cost: int) -> None:
        self.calls += cost
        self.requests += 1
        self.by_dataset[dataset_key] = self.by_dataset.get(dataset_key, 0) + cost

    def as_dict(self) -> dict[str, Any]:
        return {"api_calls": self.calls, "http_requests": self.requests, "by_dataset": dict(self.by_dataset)}


@dataclass(frozen=True)
class EodhdBar:
    code: str
    exchange: str | None
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    adjusted_close: Decimal | None
    volume: Decimal | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class EodhdSplit:
    code: str
    ex_date: date
    split_from: Decimal | None
    split_to: Decimal | None
    raw_split: str


@dataclass(frozen=True)
class EodhdDividend:
    code: str
    ex_date: date
    value: Decimal | None
    unadjusted_value: Decimal | None
    currency: str | None
    declaration_date: date | None
    record_date: date | None
    payment_date: date | None
    period: str | None


@dataclass(frozen=True)
class EodhdSymbol:
    code: str
    name: str | None
    exchange: str | None
    country: str | None
    currency: str | None
    security_type: str | None
    isin: str | None


@dataclass(frozen=True)
class EodhdFetchResult:
    payload: Any
    provenance: Provenance
    body: bytes
    observed_field_names: list[str]


def _get(record: dict[str, Any], *names: str, required: bool = False) -> Any:
    for name in names:
        if name in record:
            return record[name]
    if required:
        raise EodhdFieldError(f"none of {names} is present; keys are {sorted(record)}")
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _date(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def parse_bulk_bar(record: dict[str, Any]) -> EodhdBar:
    return EodhdBar(
        code=str(_get(record, "code", "Code", required=True)),
        exchange=_get(record, "exchange_short_name", "Ex", "exchange"),
        trade_date=_date(_get(record, "date", "Date", required=True)),
        open=_decimal(_get(record, "open", "Open")),
        high=_decimal(_get(record, "high", "High")),
        low=_decimal(_get(record, "low", "Low")),
        close=_decimal(_get(record, "close", "Close")),
        adjusted_close=_decimal(_get(record, "adjusted_close", "Adjusted_close")),
        volume=_decimal(_get(record, "volume", "Volume")),
        raw=record,
    )


def parse_split(code: str, record: dict[str, Any]) -> EodhdSplit:
    raw_split = str(_get(record, "split", required=True))
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    if "/" in raw_split:
        left, _, right = raw_split.partition("/")
        try:
            numerator = Decimal(left.strip())
            denominator = Decimal(right.strip())
        except (ValueError, ArithmeticError):
            numerator = denominator = None
    return EodhdSplit(
        code=code,
        ex_date=_date(_get(record, "date", required=True)),
        # EODHD writes "new/old": 2.0/1.0 is a two-for-one.
        split_to=numerator,
        split_from=denominator,
        raw_split=raw_split,
    )


def parse_dividend(code: str, record: dict[str, Any]) -> EodhdDividend:
    return EodhdDividend(
        code=code,
        ex_date=_date(_get(record, "date", required=True)),
        value=_decimal(_get(record, "value")),
        unadjusted_value=_decimal(_get(record, "unadjustedValue")),
        currency=_get(record, "currency"),
        declaration_date=_date(_get(record, "declarationDate")),
        record_date=_date(_get(record, "recordDate")),
        payment_date=_date(_get(record, "paymentDate")),
        period=_get(record, "period"),
    )


def parse_symbol(record: dict[str, Any]) -> EodhdSymbol:
    return EodhdSymbol(
        code=str(_get(record, "Code", "code", required=True)),
        name=_get(record, "Name", "name"),
        exchange=_get(record, "Exchange", "exchange"),
        country=_get(record, "Country", "country"),
        currency=_get(record, "Currency", "currency"),
        security_type=_get(record, "Type", "type"),
        isin=_get(record, "Isin", "isin"),
    )


class EodhdProvider:
    """Reads EODHD. The token is held in memory and never logged or stored."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = EODHD_BASE,
        plan: str = "EOD Historical Data - All World",
        quota: QuotaLedger | None = None,
    ) -> None:
        if not api_token:
            raise ValueError("an EODHD API token is required; set SURGE_EODHD_API_TOKEN")
        self._token = api_token
        self._base_url = base_url.rstrip("/")
        self.plan = plan
        self.quota = quota or QuotaLedger()

    def _request(self, path: str, params: dict[str, str], dataset_key: str, cost: int) -> EodhdFetchResult:
        query = {"api_token": self._token, "fmt": "json", **params}
        url = f"{self._base_url}{path}?" + "&".join(f"{k}={v}" for k, v in query.items())
        response = fetch(url)
        self.quota.charge(dataset_key, cost)

        payload = json.loads(response.body.decode("utf-8"))
        observed: list[str] = []
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            observed = sorted(payload[0])
        elif isinstance(payload, dict):
            observed = sorted(payload)

        # The endpoint carries the token. Keep it out of the stored provenance:
        # a raw object manifest is read by people, and a secret in it is a leak.
        safe_query = {k: v for k, v in query.items() if k != "api_token"}
        provenance = Provenance(
            source_id=PROVIDER_ID,
            dataset_key=dataset_key,
            endpoint=f"{self._base_url}{path}?" + "&".join(f"{k}={v}" for k, v in safe_query.items()),
            requested_at=response.requested_at,
            received_at=response.received_at,
            http_status=response.status,
            bytes=response.bytes,
            content_sha256=response.sha256,
            item_count=len(payload) if isinstance(payload, list) else 1,
            observed_at=response.received_at,
            available_at=response.received_at,
        )
        return EodhdFetchResult(payload, provenance, response.body, observed)

    # ------------------------------------------------------------------ bars
    def fetch_us_bulk_day(self, trade_date: date | None = None) -> EodhdFetchResult:
        """Every US listing's bar for one session.

        One HTTP request, one hundred API calls. With no date the endpoint
        returns the most recent session it has.
        """

        params = {"date": trade_date.isoformat()} if trade_date else {}
        return self._request("/eod-bulk-last-day/US", params, BULK_DATASET, BULK_EXCHANGE_CALL_COST)

    def fetch_symbol_eod(self, symbol: str, *, start: date | None = None, end: date | None = None) -> EodhdFetchResult:
        params: dict[str, str] = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()
        return self._request(f"/eod/{symbol}", params, BULK_DATASET, SINGLE_CALL_COST)

    # ------------------------------------------------------ corporate actions
    def fetch_splits(self, symbol: str, *, start: date | None = None) -> EodhdFetchResult:
        params = {"from": start.isoformat()} if start else {}
        return self._request(f"/splits/{symbol}", params, SPLITS_DATASET, SINGLE_CALL_COST)

    def fetch_dividends(self, symbol: str, *, start: date | None = None) -> EodhdFetchResult:
        params = {"from": start.isoformat()} if start else {}
        return self._request(f"/div/{symbol}", params, DIVIDENDS_DATASET, SINGLE_CALL_COST)

    # ---------------------------------------------------------------- symbols
    def fetch_symbol_list(self, *, delisted: bool = False) -> EodhdFetchResult:
        """The US symbol list.

        ``delisted=1`` replaces the result set rather than extending it, so the
        caller decides which population it is asking for and gets exactly that.
        """

        params = {"delisted": "1"} if delisted else {}
        return self._request("/exchange-symbol-list/US", params, SYMBOL_LIST_DATASET, SINGLE_CALL_COST)

    def fetch_symbol_changes(self, since: date) -> EodhdFetchResult:
        return self._request(
            "/symbol-change-history", {"from": since.isoformat()}, SYMBOL_CHANGES_DATASET, SINGLE_CALL_COST
        )

    # --------------------------------------------------------------------- fx
    def fetch_usdjpy(self, *, start: date | None = None, end: date | None = None) -> EodhdFetchResult:
        """USDJPY.FOREX, the secondary FX source.

        EODHD states its forex is not exchange sourced and that the prices are
        indicative. It cross-checks the ECB cross; it does not replace it.
        """

        params: dict[str, str] = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()
        return self._request("/eod/USDJPY.FOREX", params, FX_DATASET, SINGLE_CALL_COST)
