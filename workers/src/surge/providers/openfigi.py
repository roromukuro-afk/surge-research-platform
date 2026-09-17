"""OpenFIGI mapping: research enrichment, not an identity change.

FIGI is the only share-class identifier this project can actually hold - it is
dedicated to the public domain, so storing it, deriving from it and publishing it
are all permitted, which is not true of CUSIP or of a US ISIN. That makes it the
candidate for upgrading US security identity above REGISTRY_ANCHORED.

This module does not make that upgrade. It records what OpenFIGI answered,
including every match when there is more than one, and stops. Promotion is a
separate, recorded identity migration with its own identity_version, after a
coverage report has been reviewed - because the mapping has to go through the
ticker, and the ticker is exactly the unstable thing we are trying to get away
from. FRCB, live today, returns two different issuers.

Measured behaviour this module is written against (2026-09-16, keyless):

* The response array is parallel to the request array, one element per job.
* An element is {"data": [...]}, {"warning": "..."} or {"error": "..."}.
* Rate limit headers come back as ratelimit-limit / ratelimit-remaining /
  ratelimit-reset, with the keyless policy reported as 25 per 60 seconds.
* Share classes are written with a slash: BRK/B resolves, BRK.B and BRK-B do not.
* Delisted names need includeUnlistedEquities, and resolve only under the ticker
  FIGI holds for them (TWTR yes; a pre-rename symbol, no).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from surge.http_fetch import fetch
from surge.models import Provenance

OPENFIGI_MAPPING_URL = "https://api.openfigi.com/v3/mapping"
PROVIDER_ID = "openfigi"
DATASET_KEY = "OPENFIGI_MAPPING"

# Documented and confirmed live: 10 jobs per request without a key, 100 with one.
MAX_JOBS_KEYLESS = 10
MAX_JOBS_WITH_KEY = 100


class MappingStatus(StrEnum):
    EXACT = "EXACT"
    AMBIGUOUS = "AMBIGUOUS"
    UNMAPPED = "UNMAPPED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class MappingRequest:
    id_type: str
    id_value: str
    exch_code: str | None = None
    mic_code: str | None = None
    security_type2: str | None = None
    include_unlisted_equities: bool = False

    def to_job(self) -> dict[str, object]:
        job: dict[str, object] = {"idType": self.id_type, "idValue": self.id_value}
        if self.exch_code:
            job["exchCode"] = self.exch_code
        if self.mic_code:
            job["micCode"] = self.mic_code
        if self.security_type2:
            job["securityType2"] = self.security_type2
        if self.include_unlisted_equities:
            job["includeUnlistedEquities"] = True
        return job


@dataclass(frozen=True)
class FigiMatch:
    figi: str | None
    composite_figi: str | None
    share_class_figi: str | None
    ticker: str | None
    exch_code: str | None
    security_type: str | None
    security_type2: str | None
    name: str | None
    security_description: str | None
    market_sector: str | None

    @classmethod
    def from_payload(cls, payload: dict) -> FigiMatch:
        return cls(
            figi=payload.get("figi"),
            composite_figi=payload.get("compositeFIGI"),
            share_class_figi=payload.get("shareClassFIGI"),
            ticker=payload.get("ticker"),
            exch_code=payload.get("exchCode"),
            security_type=payload.get("securityType"),
            security_type2=payload.get("securityType2"),
            name=payload.get("name"),
            security_description=payload.get("securityDescription"),
            market_sector=payload.get("marketSector"),
        )


@dataclass(frozen=True)
class MappingOutcome:
    request: MappingRequest
    status: MappingStatus
    matches: list[FigiMatch] = field(default_factory=list)
    warning: str | None = None
    error: str | None = None

    @property
    def match_count(self) -> int:
        return len(self.matches)

    @property
    def share_class_figi(self) -> str | None:
        """Only meaningful when exactly one match came back.

        An ambiguous result has no single answer, and picking the first one is
        how a false merge happens.
        """

        if self.status is not MappingStatus.EXACT:
            return None
        return self.matches[0].share_class_figi


def ticker_candidates(symbol: str) -> list[str]:
    """The forms of a symbol worth trying, most likely first.

    Registries disagree about share-class punctuation: the SEC writes BRK-B,
    some feeds write BRK.B, OpenFIGI wants BRK/B. Rather than rewrite blindly -
    a dash is also a legitimate part of unit and warrant symbols - we try the
    symbol as supplied and fall back to the slash forms, and record which form
    actually resolved.
    """

    candidates = [symbol]
    for separator in (".", "-"):
        if separator in symbol:
            candidate = symbol.replace(separator, "/")
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


class OpenFigiProvider:
    provider_id = PROVIDER_ID
    dataset_key = DATASET_KEY

    def __init__(self, *, api_key: str | None = None, url: str = OPENFIGI_MAPPING_URL) -> None:
        self._api_key = api_key
        self._url = url
        self._last_headers: dict[str, str] = {}

    @property
    def batch_size(self) -> int:
        return MAX_JOBS_WITH_KEY if self._api_key else MAX_JOBS_KEYLESS

    @property
    def last_rate_limit(self) -> dict[str, str]:
        """The rate limit state the API reported on the most recent call."""

        return {
            key: value
            for key, value in self._last_headers.items()
            if key.lower().startswith("ratelimit")
        }

    def _post(self, jobs: list[dict[str, object]]) -> tuple[list[dict], Provenance, bytes]:
        body = json.dumps(jobs).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-OPENFIGI-APIKEY"] = self._api_key

        response = fetch(self._url, headers=headers, data=body, method="POST")
        self._last_headers = dict(response.headers)
        payload = json.loads(response.body.decode("utf-8"))

        provenance = Provenance(
            source_id=PROVIDER_ID,
            dataset_key=DATASET_KEY,
            endpoint=self._url,
            requested_at=response.requested_at,
            received_at=response.received_at,
            http_status=response.status,
            bytes=response.bytes,
            content_sha256=response.sha256,
            item_count=len(payload),
            observed_at=response.received_at,
            available_at=response.received_at,
        )
        return payload, provenance, response.body

    def _respect_rate_limit(self) -> None:
        """Wait when the API says we are out of allowance.

        The headers are authoritative and cheap; sleeping on our own guess would
        be slower when we are fine and still too fast when we are not.
        """

        remaining = self._last_headers.get("ratelimit-remaining") or self._last_headers.get(
            "RateLimit-Remaining"
        )
        reset = self._last_headers.get("ratelimit-reset") or self._last_headers.get("RateLimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            if int(remaining) <= 1:
                time.sleep(min(float(reset) + 1.0, 120.0))
        except ValueError:  # pragma: no cover - a header we do not recognise
            return

    def map(self, requests: list[MappingRequest]) -> tuple[list[MappingOutcome], list[Provenance], list[bytes]]:
        """Map a list of inputs, batching to the documented job limit."""

        outcomes: list[MappingOutcome] = []
        provenances: list[Provenance] = []
        bodies: list[bytes] = []

        for start in range(0, len(requests), self.batch_size):
            batch = requests[start : start + self.batch_size]
            payload, provenance, body = self._post([r.to_job() for r in batch])
            provenances.append(provenance)
            bodies.append(body)

            if len(payload) != len(batch):
                raise RuntimeError(
                    f"OpenFIGI returned {len(payload)} results for {len(batch)} jobs; "
                    "the response is documented as parallel to the request and cannot be aligned"
                )

            for request, element in zip(batch, payload, strict=True):
                outcomes.append(_interpret(request, element))

            if start + self.batch_size < len(requests):
                self._respect_rate_limit()

        return outcomes, provenances, bodies

    def map_symbol(
        self, symbol: str, *, exch_code: str = "US", include_unlisted_equities: bool = False
    ) -> tuple[MappingOutcome, list[Provenance], list[bytes]]:
        """Try each punctuation form of a symbol until one resolves.

        Returns the first resolving outcome, or the outcome for the symbol as
        supplied when none of them resolve - so an unmapped result is reported
        against the form we were actually given.
        """

        provenances: list[Provenance] = []
        bodies: list[bytes] = []
        first: MappingOutcome | None = None

        for candidate in ticker_candidates(symbol):
            request = MappingRequest(
                id_type="TICKER",
                id_value=candidate,
                exch_code=exch_code,
                include_unlisted_equities=include_unlisted_equities,
            )
            outcomes, prov, body = self.map([request])
            provenances.extend(prov)
            bodies.extend(body)
            outcome = outcomes[0]
            first = first or outcome
            if outcome.status in (MappingStatus.EXACT, MappingStatus.AMBIGUOUS):
                return outcome, provenances, bodies

        assert first is not None
        return first, provenances, bodies


def _interpret(request: MappingRequest, element: dict) -> MappingOutcome:
    if "error" in element:
        return MappingOutcome(request, MappingStatus.ERROR, error=str(element["error"]))
    if "warning" in element:
        return MappingOutcome(request, MappingStatus.UNMAPPED, warning=str(element["warning"]))

    data = element.get("data") or []
    matches = [FigiMatch.from_payload(row) for row in data]
    if not matches:
        # A data array that is present but empty is not documented; treat it as
        # unmapped rather than inventing a match.
        return MappingOutcome(request, MappingStatus.UNMAPPED, warning="empty data array")
    status = MappingStatus.EXACT if len(matches) == 1 else MappingStatus.AMBIGUOUS
    return MappingOutcome(request, status, matches=matches)


def coverage(outcomes: list[MappingOutcome]) -> dict[str, int]:
    """The coverage report Phase 2 owes before any identity promotion."""

    summary = {status.value.lower(): 0 for status in MappingStatus}
    summary["inputs"] = len(outcomes)
    for outcome in outcomes:
        summary[outcome.status.value.lower()] += 1
    return summary


def requested_at_of(provenances: list[Provenance]) -> datetime | None:
    return min((p.requested_at for p in provenances), default=None)
