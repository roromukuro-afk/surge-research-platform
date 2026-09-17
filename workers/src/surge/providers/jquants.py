"""J-Quants v2: Tokyo Stock Exchange daily bars and the issue master.

Three properties of this source shape the adapter.

*Unadjusted and adjusted arrive together.* The bar carries as-traded O/H/L/C and
volume alongside an adjusted series and the day's adjustment factor. They are
split into two tables on the way in, because the adjusted series is recomputed
backwards through history every time a split happens - with, the documentation
says, no limit on how far back - so it is not a historical fact and must never
sit in the same row as one.

*Corrections are silent overwrites.* J-Quants publishes no version, no ETag and
no diff, and states that a correction replaces the existing data. The only
defence is to refetch a rolling window and compare digests, which the ingestion
job does, recording what changed.

*There is no delisting list and no code-change table.* The issue master snapshot
for a date is the only evidence of either, so every snapshot is stored from day
one; a snapshot not taken is a fact permanently lost.

The field names below are JPX's own, read from the V2 specification pages on
2026-09-17. The V1 names are kept as a fallback because the wire could not be
measured without a subscription - every J-Quants endpoint, including on the free
plan, requires a key issued from the logged-in dashboard. A field that matches
neither is an error, never a silent null, and the smoke test prints the keys the
API actually returned so any difference shows up on the first real call.

One documentation quirk worth knowing: the spec marks every field "Required",
while its own footnotes say the morning and afternoon session keys are absent
entirely on non-Premium plans, and that MktCap and ExRT are null on days with no
trade and no corporate action. "Required" there means documented, not present.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from surge.http_fetch import fetch
from surge.models import Provenance

JQUANTS_BASE = "https://api.jquants.com/v2"
PROVIDER_ID = "jquants"
BARS_DATASET = "JQ_EQ_BARS_DAILY"
MASTER_DATASET = "JQ_EQ_MASTER"

# Documented plan limits, Standard: 120 requests/minute.
_MIN_INTERVAL_SECONDS = 0.5


class JQuantsFieldError(RuntimeError):
    """A documented field was not in the response. Reported, never worked around."""


# V2 short names first, V1 names as fallback. Both name the same quantity;
# where they would not, the field is absent from this map on purpose.
BAR_FIELDS: dict[str, tuple[str, ...]] = {
    "code": ("Code",),
    "trade_date": ("Date",),
    "open": ("O", "Open"),
    "high": ("H", "High"),
    "low": ("L", "Low"),
    "close": ("C", "Close"),
    "volume": ("Vo", "Volume"),
    "turnover": ("Va", "TurnoverValue"),
    # Limit up / limit down flags, documented as the STRINGS "0" and "1".
    "limit_up": ("UL",),
    "limit_down": ("LL",),
    # Unadjusted close x shares outstanding, in millions of yen. Null for ETFs
    # and ETNs, and on days with no trade.
    "market_cap_million_jpy": ("MktCap",),
}

# Premium only: on every other plan these keys are absent from the response
# rather than null. Named here so the smoke test can report whether they came.
MORNING_SESSION_FIELDS = ("MO", "MH", "ML", "MC", "MUL", "MLL", "MVo", "MVa",
                          "MAdjO", "MAdjH", "MAdjL", "MAdjC", "MAdjVo")
AFTERNOON_SESSION_FIELDS = ("AO", "AH", "AL", "AC", "AUL", "ALL", "AVo", "AVa",
                            "AAdjO", "AAdjH", "AAdjL", "AAdjC", "AAdjVo")

ADJUSTED_BAR_FIELDS: dict[str, tuple[str, ...]] = {
    "adj_open": ("AdjO", "AdjustmentOpen"),
    "adj_high": ("AdjH", "AdjustmentHigh"),
    "adj_low": ("AdjL", "AdjustmentLow"),
    "adj_close": ("AdjC", "AdjustmentClose"),
    "adj_volume": ("AdjVo", "AdjustmentVolume"),
    "adjustment_factor": ("AdjFactor", "AdjustmentFactor"),
    "ex_event_code": ("ExRT",),
}

MASTER_FIELDS: dict[str, tuple[str, ...]] = {
    "code": ("Code",),
    "as_of_date": ("Date",),
    "name": ("CoName", "CompanyName"),
    "name_en": ("CoNameEn", "CompanyNameEnglish"),
    "market_code": ("Mkt", "MarketCode"),
    "market_name": ("MktNm", "MarketCodeName"),
    "sector17": ("S17", "Sector17Code"),
    "sector17_name": ("S17Nm", "Sector17CodeName"),
    "sector33": ("S33", "Sector33Code"),
    "sector33_name": ("S33Nm", "Sector33CodeName"),
    "scale_category": ("ScaleCat", "ScaleCategory"),
    "margin_code": ("Mrgn", "MarginCode"),
    "margin_name": ("MrgnNm", "MarginCodeName"),
    # Added by JPX on 2026-05-26. The first J-Quants field that separates an
    # ordinary share from a REIT, an ETF or a preferred investment certificate
    # without reading the issue name - which is how Phase 1 had to do it.
    "product_category": ("ProdCat",),
}

# ProdCat, from the official code table.
PRODUCT_CATEGORY = {
    "011": "DOMESTIC_SHARE",
    "012": "PREFERRED_INVESTMENT_CERTIFICATE",
    "013": "REIT",
    "014": "ETF",
    "021": "FOREIGN_SHARE",
    "022": "FOREIGN_REIT",
    "023": "FOREIGN_ETF",
    "024": "FOREIGN_DEPOSITARY_RECEIPT",
}

# Market segment codes. Not a dense range: 0103, 0108 and 0110 do not exist.
MARKET_SEGMENT = {
    "0101": "TSE 1st Section",
    "0102": "TSE 2nd Section",
    "0104": "Mothers",
    "0105": "TOKYO PRO MARKET",
    "0106": "JASDAQ Standard",
    "0107": "JASDAQ Growth",
    "0109": "Other",
    "0111": "Prime",
    "0112": "Standard",
    "0113": "Growth",
}

# ExRT, documented: 1 split, 2 reverse split, 3 rights issue. A gratis allotment
# of new shares is reported inside 1.
EX_EVENT_MEANING = {
    "1": "SPLIT",
    "2": "REVERSE_SPLIT",
    "3": "RIGHTS_ISSUE",
}


@dataclass(frozen=True)
class JQuantsBar:
    code: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    turnover: Decimal | None
    adj_open: Decimal | None
    adj_high: Decimal | None
    adj_low: Decimal | None
    adj_close: Decimal | None
    adj_volume: Decimal | None
    adjustment_factor: Decimal | None
    ex_event_code: str | None
    limit_up: str | None = None
    limit_down: str | None = None
    market_cap_million_jpy: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_five_digit_code(self) -> bool:
        return len(self.code) == 5

    @property
    def has_session_detail(self) -> bool:
        """Whether the Premium-only morning and afternoon keys arrived at all."""

        return any(name in self.raw for name in MORNING_SESSION_FIELDS)


@dataclass(frozen=True)
class JQuantsMasterRecord:
    code: str
    name: str | None
    name_en: str | None
    market_code: str | None
    market_name: str | None
    sector17: str | None
    sector33: str | None
    scale_category: str | None
    as_of_date: str | None = None
    sector17_name: str | None = None
    sector33_name: str | None = None
    margin_code: str | None = None
    margin_name: str | None = None
    product_category: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def product_category_meaning(self) -> str | None:
        """What ProdCat says this instrument is, or None when it says nothing.

        An unrecognised code returns UNKNOWN_PRODUCT_CATEGORY rather than a
        guess: JPX added this field in 2026 and can add codes to it.
        """

        if self.product_category is None:
            return None
        return PRODUCT_CATEGORY.get(self.product_category, "UNKNOWN_PRODUCT_CATEGORY")


@dataclass(frozen=True)
class JQuantsFetchResult:
    records: list[dict[str, Any]]
    provenance: Provenance
    body: bytes
    observed_field_names: list[str]
    pages: int


def _pick(record: dict[str, Any], names: tuple[str, ...], *, required: bool) -> Any:
    for name in names:
        if name in record:
            return record[name]
    if required:
        raise JQuantsFieldError(
            f"none of {names} is present in the response record; keys are {sorted(record)}"
        )
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def parse_bar(record: dict[str, Any]) -> JQuantsBar:
    return JQuantsBar(
        code=str(_pick(record, BAR_FIELDS["code"], required=True)),
        trade_date=date.fromisoformat(str(_pick(record, BAR_FIELDS["trade_date"], required=True))[:10]),
        open=_decimal(_pick(record, BAR_FIELDS["open"], required=False)),
        high=_decimal(_pick(record, BAR_FIELDS["high"], required=False)),
        low=_decimal(_pick(record, BAR_FIELDS["low"], required=False)),
        close=_decimal(_pick(record, BAR_FIELDS["close"], required=False)),
        volume=_decimal(_pick(record, BAR_FIELDS["volume"], required=False)),
        turnover=_decimal(_pick(record, BAR_FIELDS["turnover"], required=False)),
        adj_open=_decimal(_pick(record, ADJUSTED_BAR_FIELDS["adj_open"], required=False)),
        adj_high=_decimal(_pick(record, ADJUSTED_BAR_FIELDS["adj_high"], required=False)),
        adj_low=_decimal(_pick(record, ADJUSTED_BAR_FIELDS["adj_low"], required=False)),
        adj_close=_decimal(_pick(record, ADJUSTED_BAR_FIELDS["adj_close"], required=False)),
        adj_volume=_decimal(_pick(record, ADJUSTED_BAR_FIELDS["adj_volume"], required=False)),
        adjustment_factor=_decimal(_pick(record, ADJUSTED_BAR_FIELDS["adjustment_factor"], required=False)),
        ex_event_code=_optional_str(_pick(record, ADJUSTED_BAR_FIELDS["ex_event_code"], required=False)),
        limit_up=_optional_str(_pick(record, BAR_FIELDS["limit_up"], required=False)),
        limit_down=_optional_str(_pick(record, BAR_FIELDS["limit_down"], required=False)),
        market_cap_million_jpy=_decimal(_pick(record, BAR_FIELDS["market_cap_million_jpy"], required=False)),
        raw=record,
    )


def parse_master(record: dict[str, Any]) -> JQuantsMasterRecord:
    return JQuantsMasterRecord(
        code=str(_pick(record, MASTER_FIELDS["code"], required=True)),
        name=_optional_str(_pick(record, MASTER_FIELDS["name"], required=False)),
        name_en=_optional_str(_pick(record, MASTER_FIELDS["name_en"], required=False)),
        market_code=_optional_str(_pick(record, MASTER_FIELDS["market_code"], required=False)),
        market_name=_optional_str(_pick(record, MASTER_FIELDS["market_name"], required=False)),
        sector17=_optional_str(_pick(record, MASTER_FIELDS["sector17"], required=False)),
        sector33=_optional_str(_pick(record, MASTER_FIELDS["sector33"], required=False)),
        scale_category=_optional_str(_pick(record, MASTER_FIELDS["scale_category"], required=False)),
        as_of_date=_optional_str(_pick(record, MASTER_FIELDS["as_of_date"], required=False)),
        sector17_name=_optional_str(_pick(record, MASTER_FIELDS["sector17_name"], required=False)),
        sector33_name=_optional_str(_pick(record, MASTER_FIELDS["sector33_name"], required=False)),
        margin_code=_optional_str(_pick(record, MASTER_FIELDS["margin_code"], required=False)),
        margin_name=_optional_str(_pick(record, MASTER_FIELDS["margin_name"], required=False)),
        product_category=_optional_str(_pick(record, MASTER_FIELDS["product_category"], required=False)),
        raw=record,
    )


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def extract_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Find the record list in the response envelope.

    V2 returns records under "data"; V1 used a per-endpoint key. Prefer the
    documented V2 key, otherwise take the one list of objects the payload
    contains - and refuse if there is more than one, because an ambiguous
    envelope is not the envelope this adapter was written for.
    """

    if isinstance(payload.get("data"), list):
        return payload["data"], "data"

    candidates = {
        key: value
        for key, value in payload.items()
        if isinstance(value, list) and (not value or isinstance(value[0], dict))
    }
    if len(candidates) == 1:
        key, value = next(iter(candidates.items()))
        return value, key
    if not candidates:
        raise JQuantsFieldError(f"no record list in the response; keys are {sorted(payload)}")
    raise JQuantsFieldError(
        f"more than one candidate record list in the response ({sorted(candidates)}); "
        "the envelope is not what this adapter was written for"
    )


class JQuantsProvider:
    """Reads J-Quants. Holds the key in memory only, and never logs it."""

    provider_id = PROVIDER_ID

    def __init__(self, api_key: str, *, base_url: str = JQUANTS_BASE, plan: str = "Standard") -> None:
        if not api_key:
            raise ValueError("a J-Quants API key is required; set SURGE_JQUANTS_API_KEY")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.plan = plan

    def _get(self, path: str, params: dict[str, str], dataset_key: str) -> JQuantsFetchResult:
        records: list[dict[str, Any]] = []
        bodies: list[bytes] = []
        pagination_key: str | None = None
        pages = 0
        first_requested_at = None
        last_received_at = None
        status = 0
        total_bytes = 0
        observed_keys: list[str] = []

        while True:
            query = dict(params)
            if pagination_key:
                query["pagination_key"] = pagination_key
            url = f"{self._base_url}{path}?" + "&".join(f"{k}={v}" for k, v in query.items())

            response = fetch(
                url,
                headers={"x-api-key": self._api_key},
                min_interval=_MIN_INTERVAL_SECONDS,
            )
            first_requested_at = first_requested_at or response.requested_at
            last_received_at = response.received_at
            status = response.status
            total_bytes += response.bytes
            bodies.append(response.body)
            pages += 1

            payload = json.loads(response.body.decode("utf-8"))
            page_records, _envelope_key = extract_records(payload)
            if page_records and not observed_keys:
                observed_keys = sorted(page_records[0])
            records.extend(page_records)

            pagination_key = payload.get("pagination_key")
            if not pagination_key:
                break

        # One object per logical request: the concatenated pages are what the
        # request returned, and hashing them together is what makes a rolling
        # refetch comparable.
        body = b"\n".join(bodies)
        provenance = Provenance(
            source_id=PROVIDER_ID,
            dataset_key=dataset_key,
            endpoint=f"{self._base_url}{path}?" + "&".join(f"{k}={v}" for k, v in params.items()),
            requested_at=first_requested_at,
            received_at=last_received_at,
            http_status=status,
            bytes=total_bytes,
            content_sha256=hashlib.sha256(body).hexdigest(),
            item_count=len(records),
            observed_at=last_received_at,
            available_at=last_received_at,
        )
        return JQuantsFetchResult(records, provenance, body, observed_keys, pages)

    def fetch_daily_bars(self, trade_date: date) -> JQuantsFetchResult:
        """Every listed issue's bar for one session, in one logical request.

        J-Quants asks callers not to loop over issues, and a date-only request
        is also what makes the result survivorship-bias free by construction:
        whatever traded that day is in it, including names now delisted.
        """

        return self._get("/equities/bars/daily", {"date": trade_date.isoformat()}, BARS_DATASET)

    def fetch_master(self, as_of: date) -> JQuantsFetchResult:
        """The issue master as of a date.

        Stored every day without exception: J-Quants publishes no delisting list
        and no code-change mapping, so the diff between consecutive snapshots is
        the only place either becomes visible.
        """

        return self._get("/equities/master", {"date": as_of.isoformat()}, MASTER_DATASET)
