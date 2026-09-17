"""USD/JPY from the ECB's euro reference rates.

The ECB publishes no USD/JPY series. It publishes USD per EUR and JPY per EUR,
and the cross is

    USD/JPY = (JPY per EUR) / (USD per EUR)

Both legs are stored raw next to the quotient, for two reasons. The legs are
published to different precisions - USD/EUR to four decimals, JPY/EUR to two -
so the quotient carries a rounding error that only the legs can explain. And the
ECB's own terms require that a modification of its data be stated explicitly;
deriving a cross is such a modification, so the derivation is recorded as data
rather than buried in whichever query happens to compute it.

Two things this module deliberately does not do:

* It does not assume a rate is final. The ECB states it will not amend a rate
  once the next business day's rate is out, but a stated policy is not a
  guarantee, so the daily job refetches a rolling window and compares digests.
* It does not convert publication time into a market time zone. The response
  carries Last-Modified; that timestamp is stored as the provider's own claim,
  and ``available_at`` remains the moment we actually received it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from surge.http_fetch import fetch
from surge.licensing import AvailabilityBasis
from surge.models import Provenance

ECB_BASE = "https://data-api.ecb.europa.eu/service/data/EXR"
DATASET_KEY = "ECB_EXR_DAILY"
PROVIDER_ID = "ecb"

# One request returns both legs: the key's CURRENCY dimension accepts a list.
BOTH_LEGS_KEY = "D.USD+JPY.EUR.SP00.A"

USD_SERIES = "EXR.D.USD.EUR.SP00.A"
JPY_SERIES = "EXR.D.JPY.EUR.SP00.A"

# The cross is quoted to the precision of the less precise leg plus a margin;
# six decimals is far finer than any 3,000 JPY eligibility decision needs and
# keeps the stored number reproducible from the stored legs.
CROSS_DECIMALS = Decimal("0.000001")

DERIVATION_METHOD = "USDJPY = OBS_VALUE(EXR.D.JPY.EUR.SP00.A) / OBS_VALUE(EXR.D.USD.EUR.SP00.A)"


@dataclass(frozen=True)
class EcbObservation:
    series_key: str
    currency: str
    period: date
    value: Decimal
    decimals: int
    obs_status: str
    title_compl: str


@dataclass(frozen=True)
class UsdJpyRate:
    """One day's cross, with both legs it was built from."""

    source_date: date
    eur_usd: Decimal | None
    eur_jpy: Decimal | None
    derived_usd_jpy: Decimal | None
    eur_usd_decimals: int | None
    eur_jpy_decimals: int | None
    derivation_method: str
    note: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.derived_usd_jpy is not None


@dataclass(frozen=True)
class EcbFetchResult:
    observations: list[EcbObservation]
    rates: list[UsdJpyRate]
    provenance: Provenance
    body: bytes
    source_published_at: datetime | None
    not_modified: bool = False


def _parse_csvdata(body: bytes) -> list[EcbObservation]:
    """Parse the SDMX csvdata response by column NAME.

    The response carries thirty-odd attribute columns and the ECB is free to add
    more; reading by position would break silently the first time it did.
    """

    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    observations: list[EcbObservation] = []
    for row in reader:
        raw_value = (row.get("OBS_VALUE") or "").strip()
        period = (row.get("TIME_PERIOD") or "").strip()
        if not raw_value or not period:
            # A published gap (a TARGET closing day) carries no value. It is not
            # an error and it is not a zero.
            continue
        decimals = (row.get("DECIMALS") or "").strip()
        observations.append(
            EcbObservation(
                series_key=(row.get("KEY") or "").strip(),
                currency=(row.get("CURRENCY") or "").strip(),
                period=date.fromisoformat(period),
                value=Decimal(raw_value),
                decimals=int(decimals) if decimals.isdigit() else -1,
                obs_status=(row.get("OBS_STATUS") or "").strip(),
                title_compl=(row.get("TITLE_COMPL") or "").strip(),
            )
        )
    return observations


def derive_usdjpy(observations: list[EcbObservation]) -> list[UsdJpyRate]:
    """Pair the two legs by date and divide. Never carry a leg across dates."""

    usd = {o.period: o for o in observations if o.series_key == USD_SERIES}
    jpy = {o.period: o for o in observations if o.series_key == JPY_SERIES}

    rates: list[UsdJpyRate] = []
    for period in sorted(set(usd) | set(jpy)):
        usd_leg = usd.get(period)
        jpy_leg = jpy.get(period)
        if usd_leg is None or jpy_leg is None:
            # One leg without the other is not a rate. Recording the incomplete
            # day keeps the gap visible instead of letting a later reader assume
            # the date simply had no data.
            missing = "USD/EUR" if usd_leg is None else "JPY/EUR"
            rates.append(
                UsdJpyRate(
                    source_date=period,
                    eur_usd=usd_leg.value if usd_leg else None,
                    eur_jpy=jpy_leg.value if jpy_leg else None,
                    derived_usd_jpy=None,
                    eur_usd_decimals=usd_leg.decimals if usd_leg else None,
                    eur_jpy_decimals=jpy_leg.decimals if jpy_leg else None,
                    derivation_method=DERIVATION_METHOD,
                    note=f"no cross: the {missing} leg was not published for this date",
                )
            )
            continue

        cross = (jpy_leg.value / usd_leg.value).quantize(CROSS_DECIMALS, rounding=ROUND_HALF_UP)
        rates.append(
            UsdJpyRate(
                source_date=period,
                eur_usd=usd_leg.value,
                eur_jpy=jpy_leg.value,
                derived_usd_jpy=cross,
                eur_usd_decimals=usd_leg.decimals,
                eur_jpy_decimals=jpy_leg.decimals,
                derivation_method=DERIVATION_METHOD,
            )
        )
    return rates


class EcbFxProvider:
    provider_id = PROVIDER_ID
    dataset_key = DATASET_KEY

    def __init__(self, *, base_url: str = ECB_BASE, user_agent: str | None = None) -> None:
        self._base_url = base_url
        self._user_agent = user_agent

    def _url(self, *, start: date | None, end: date | None, last_n: int | None) -> str:
        params = ["format=csvdata"]
        if last_n is not None:
            params.append(f"lastNObservations={last_n}")
        if start is not None:
            params.append(f"startPeriod={start.isoformat()}")
        if end is not None:
            params.append(f"endPeriod={end.isoformat()}")
        return f"{self._base_url}/{BOTH_LEGS_KEY}?{'&'.join(params)}"

    def fetch(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        last_n: int | None = None,
        if_modified_since: str | None = None,
    ) -> EcbFetchResult:
        """Fetch both legs in one request.

        ``if_modified_since`` turns a rolling refetch into a conditional one: the
        ECB answers 304 when nothing has changed, which is both cheaper and a
        clearer signal than comparing bodies.
        """

        url = self._url(start=start, end=end, last_n=last_n)
        headers = {"If-Modified-Since": if_modified_since} if if_modified_since else None
        kwargs = {"user_agent": self._user_agent} if self._user_agent else {}
        response = fetch(url, headers=headers, accept_statuses=(304,), **kwargs)

        if response.status == 304:
            provenance = Provenance(
                source_id=PROVIDER_ID,
                dataset_key=DATASET_KEY,
                endpoint=url,
                requested_at=response.requested_at,
                received_at=response.received_at,
                http_status=304,
                bytes=0,
                content_sha256=None,
                item_count=0,
                observed_at=response.received_at,
                available_at=response.received_at,
            )
            return EcbFetchResult([], [], provenance, b"", response.last_modified, not_modified=True)

        observations = _parse_csvdata(response.body)
        rates = derive_usdjpy(observations)
        provenance = Provenance(
            source_id=PROVIDER_ID,
            dataset_key=DATASET_KEY,
            endpoint=url,
            requested_at=response.requested_at,
            received_at=response.received_at,
            http_status=response.status,
            bytes=response.bytes,
            content_sha256=response.sha256,
            item_count=len(observations),
            observed_at=response.received_at,
            # We could act on this the moment it arrived, not when the ECB
            # published it. source_published_at carries the ECB's own claim.
            available_at=response.received_at,
            source_published_at=response.last_modified,
        )
        return EcbFetchResult(observations, rates, provenance, response.body, response.last_modified)


# Ingestion always records what IT knew, and when. A replay that wants to model
# "when could the market have known this" uses source_published_at and declares
# the different basis explicitly.
INGESTION_AVAILABILITY_BASIS = AvailabilityBasis.OBSERVED_NOW
