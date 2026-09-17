"""Phase 2.1 smoke test: a few real calls to every provider, before any backfill.

The point is not coverage. It is to find out, cheaply and early, where the
documentation and the wire disagree - which field names actually come back,
whether a conditional write is really refused, whether a delisted name is really
retrievable - while the cost of being wrong is one request rather than ten years
of every listed security.

Nothing here writes ten years of anything. Each check makes a handful of calls,
stores what it got under a content-addressed key, and reports what it saw,
including the provider's own field names so a mismatch with the documentation is
visible rather than inferred.

Checks whose credentials are absent are reported as SKIPPED with the variable
that was missing. A skipped check is never reported as a pass.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from surge.licensing import AvailabilityBasis, dataset
from surge.manifest import RawObjectRecord, build_manifest_record
from surge.models import Provenance
from surge.storage import ObjectStore, open_store
from surge.storage.base import ImmutableObjectConflict


class CheckStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    objects: list[RawObjectRecord] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "evidence": self.evidence,
            "objects": [o.object_key for o in self.objects],
        }


@dataclass
class SmokeReport:
    started_at: datetime
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> CheckResult:
        self.checks.append(result)
        return result

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.FAILED]

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "passed": sum(1 for c in self.checks if c.status is CheckStatus.PASSED),
            "failed": len(self.failed),
            "skipped": sum(1 for c in self.checks if c.status is CheckStatus.SKIPPED),
            "checks": [c.as_dict() for c in self.checks],
        }


def _store_payload(
    store: ObjectStore,
    provenance: Provenance,
    body: bytes,
    *,
    policy_version: str,
    plan: str,
    data_from: date | None = None,
    data_to: date | None = None,
) -> RawObjectRecord:
    spec = dataset(provenance.dataset)
    stored = store.put_content_addressed(
        provenance.source_id, provenance.dataset, body, spec.content_type, spec.extension
    )
    return build_manifest_record(
        stored=stored,
        provenance=provenance,
        license_policy_version=policy_version,
        entitlement_plan=plan,
        availability_basis=AvailabilityBasis.OBSERVED_NOW,
        data_from=data_from,
        data_to=data_to,
    )


# --------------------------------------------------------------------- ECB
def check_ecb(store: ObjectStore) -> CheckResult:
    from surge.providers.ecb_fx import EcbFxProvider

    provider = EcbFxProvider()
    try:
        latest = provider.fetch(last_n=5)
    except Exception as exc:  # noqa: BLE001 - a smoke check reports, it does not raise
        return CheckResult("ecb.reference_rates", CheckStatus.FAILED, f"{type(exc).__name__}: {exc}")

    complete = [r for r in latest.rates if r.is_complete]
    if not complete:
        return CheckResult("ecb.reference_rates", CheckStatus.FAILED, "no complete cross in the last 5 observations")

    # A past window, to prove the same parse works away from the latest edge.
    past_start = date(2026, 1, 5)
    past = provider.fetch(start=past_start, end=past_start + timedelta(days=7))
    past_complete = [r for r in past.rates if r.is_complete]

    # The documented reproducibility property, tested rather than assumed.
    conditional = None
    if latest.source_published_at:
        header = latest.source_published_at.strftime("%a, %d %b %Y %H:%M:%S GMT")
        conditional = provider.fetch(last_n=5, if_modified_since=header)

    newest = complete[-1]
    record = _store_payload(
        store,
        latest.provenance,
        latest.body,
        policy_version="ecb-2026-09-16",
        plan="public",
        data_from=min(r.source_date for r in latest.rates),
        data_to=max(r.source_date for r in latest.rates),
    )

    return CheckResult(
        "ecb.reference_rates",
        CheckStatus.PASSED,
        f"{len(latest.observations)} observations, {len(complete)} complete crosses",
        evidence={
            "latest_source_date": newest.source_date.isoformat(),
            "eur_usd": str(newest.eur_usd),
            "eur_jpy": str(newest.eur_jpy),
            "derived_usd_jpy": str(newest.derived_usd_jpy),
            "eur_usd_decimals": newest.eur_usd_decimals,
            "eur_jpy_decimals": newest.eur_jpy_decimals,
            "derivation_method": newest.derivation_method,
            "source_published_at": latest.source_published_at.isoformat() if latest.source_published_at else None,
            "past_window_complete_days": len(past_complete),
            "conditional_refetch_status": conditional.provenance.http_status if conditional else None,
            "incomplete_days": [
                {"date": r.source_date.isoformat(), "note": r.note} for r in latest.rates if not r.is_complete
            ],
        },
        objects=[record],
    )


# ---------------------------------------------------------------- OpenFIGI
def check_openfigi(store: ObjectStore) -> CheckResult:
    from surge.providers.openfigi import MappingRequest, OpenFigiProvider, coverage

    provider = OpenFigiProvider(api_key=os.environ.get("SURGE_OPENFIGI_API_KEY") or None)
    sample = [
        MappingRequest("TICKER", "AAPL", exch_code="US"),
        MappingRequest("TICKER", "MSFT", exch_code="US"),
        MappingRequest("TICKER", "BRK/B", exch_code="US"),
        MappingRequest("TICKER", "F", exch_code="US"),
        MappingRequest("TICKER", "TWTR", exch_code="US", include_unlisted_equities=True),
        MappingRequest("TICKER", "FRCB", exch_code="US", include_unlisted_equities=True),
        MappingRequest("TICKER", "ZZZZNOTREAL", exch_code="US"),
    ]

    try:
        outcomes, provenances, bodies = provider.map(sample)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("openfigi.mapping", CheckStatus.FAILED, f"{type(exc).__name__}: {exc}")

    records = [
        _store_payload(store, prov, body, policy_version="openfigi-2026-09-16", plan="public")
        for prov, body in zip(provenances, bodies, strict=True)
    ]

    by_input = {
        o.request.id_value: {
            "status": str(o.status),
            "matches": o.match_count,
            "share_class_figi": o.share_class_figi,
            "names": [m.name for m in o.matches],
        }
        for o in outcomes
    }

    # The two behaviours that matter for identity work, asserted rather than hoped.
    problems = []
    if by_input.get("AAPL", {}).get("status") != "EXACT":
        problems.append("AAPL did not resolve to a single match")
    if by_input.get("FRCB", {}).get("status") != "AMBIGUOUS":
        problems.append("FRCB no longer demonstrates ticker ambiguity")
    if by_input.get("ZZZZNOTREAL", {}).get("status") != "UNMAPPED":
        problems.append("a nonsense ticker did not come back unmapped")

    return CheckResult(
        "openfigi.mapping",
        CheckStatus.FAILED if problems else CheckStatus.PASSED,
        "; ".join(problems) or f"{len(outcomes)} inputs mapped",
        evidence={
            "coverage": coverage(outcomes),
            "by_input": by_input,
            "rate_limit_headers": provider.last_rate_limit,
            "batch_size": provider.batch_size,
        },
        objects=records,
    )


# ---------------------------------------------------------------- J-Quants
def check_jquants(store: ObjectStore) -> CheckResult:
    api_key = os.environ.get("SURGE_JQUANTS_API_KEY")
    if not api_key:
        return CheckResult(
            "jquants.daily_bars",
            CheckStatus.SKIPPED,
            "SURGE_JQUANTS_API_KEY is not set; a J-Quants Standard subscription is needed to run this check",
        )

    from surge.providers.jquants import JQuantsProvider, parse_bar, parse_master

    provider = JQuantsProvider(api_key, plan=os.environ.get("SURGE_JQUANTS_PLAN", "Standard"))
    target = _previous_business_day(date.today())

    try:
        bars = provider.fetch_daily_bars(target)
        master = provider.fetch_master(target)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("jquants.daily_bars", CheckStatus.FAILED, f"{type(exc).__name__}: {exc}")

    records = [
        _store_payload(
            store, bars.provenance, bars.body, policy_version="jquants-2026-09-16",
            plan=provider.plan, data_from=target, data_to=target,
        ),
        _store_payload(
            store, master.provenance, master.body, policy_version="jquants-2026-09-16",
            plan=provider.plan, data_from=target, data_to=target,
        ),
    ]

    parsed = [parse_bar(r) for r in bars.records[:50]]
    parsed_master = [parse_master(r) for r in master.records[:50]]
    with_ex_event = [b for b in parsed if b.ex_event_code and b.ex_event_code not in ("0", "")]
    five_digit = [b for b in parsed if b.is_five_digit_code]

    return CheckResult(
        "jquants.daily_bars",
        CheckStatus.PASSED,
        f"{len(bars.records)} bars and {len(master.records)} master rows for {target}",
        evidence={
            "trade_date": target.isoformat(),
            "bar_count": len(bars.records),
            "master_count": len(master.records),
            "bar_field_names_returned": bars.observed_field_names,
            "master_field_names_returned": master.observed_field_names,
            "pages": {"bars": bars.pages, "master": master.pages},
            "sample_bar": _sample(parsed),
            "sample_master": _sample(parsed_master),
            "rows_with_ex_event": len(with_ex_event),
            "five_digit_codes_in_sample": len(five_digit),
        },
        objects=records,
    )


# ------------------------------------------------------------------- EODHD
def check_eodhd(store: ObjectStore) -> CheckResult:
    token = os.environ.get("SURGE_EODHD_API_TOKEN")
    if not token:
        return CheckResult(
            "eodhd.us_bulk",
            CheckStatus.SKIPPED,
            "SURGE_EODHD_API_TOKEN is not set; an EODHD All World subscription is needed to run this check",
        )

    from surge.providers.eodhd import (
        EodhdProvider,
        parse_bulk_bar,
        parse_dividend,
        parse_split,
        parse_symbol,
    )

    provider = EodhdProvider(token, plan=os.environ.get("SURGE_EODHD_PLAN", "EOD Historical Data - All World"))

    try:
        bulk = provider.fetch_us_bulk_day()
        splits = provider.fetch_splits("AAPL", start=date(2014, 1, 1))
        dividends = provider.fetch_dividends("AAPL", start=date(2020, 1, 1))
        delisted = provider.fetch_symbol_list(delisted=True)
        fx = provider.fetch_usdjpy(start=date.today() - timedelta(days=10))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("eodhd.us_bulk", CheckStatus.FAILED, f"{type(exc).__name__}: {exc}")

    policy = "eodhd-2026-09-16"
    records = [
        _store_payload(store, bulk.provenance, bulk.body, policy_version=policy, plan=provider.plan),
        _store_payload(store, splits.provenance, splits.body, policy_version=policy, plan=provider.plan),
        _store_payload(store, dividends.provenance, dividends.body, policy_version=policy, plan=provider.plan),
        _store_payload(store, delisted.provenance, delisted.body, policy_version=policy, plan=provider.plan),
        _store_payload(store, fx.provenance, fx.body, policy_version=policy, plan=provider.plan),
    ]

    bars = [parse_bulk_bar(r) for r in bulk.payload[:50]] if isinstance(bulk.payload, list) else []
    split_rows = [parse_split("AAPL", r) for r in splits.payload] if isinstance(splits.payload, list) else []
    dividend_rows = (
        [parse_dividend("AAPL", r) for r in dividends.payload[:5]] if isinstance(dividends.payload, list) else []
    )
    delisted_rows = (
        [parse_symbol(r) for r in delisted.payload[:20]] if isinstance(delisted.payload, list) else []
    )

    return CheckResult(
        "eodhd.us_bulk",
        CheckStatus.PASSED,
        f"{len(bulk.payload) if isinstance(bulk.payload, list) else 0} US bars in one request",
        evidence={
            "bulk_field_names_returned": bulk.observed_field_names,
            "bulk_row_count": len(bulk.payload) if isinstance(bulk.payload, list) else None,
            "bulk_trade_date": bars[0].trade_date.isoformat() if bars else None,
            "sample_bar": _sample(bars),
            "split_field_names_returned": splits.observed_field_names,
            "aapl_splits": [
                {"ex_date": s.ex_date.isoformat(), "raw": s.raw_split, "to": str(s.split_to), "from": str(s.split_from)}
                for s in split_rows
            ],
            "dividend_field_names_returned": dividends.observed_field_names,
            "dividend_sample": [
                {
                    "ex_date": d.ex_date.isoformat(),
                    "value": str(d.value),
                    "unadjusted_value": str(d.unadjusted_value),
                    "currency": d.currency,
                }
                for d in dividend_rows
            ],
            "delisted_field_names_returned": delisted.observed_field_names,
            "delisted_count": len(delisted.payload) if isinstance(delisted.payload, list) else None,
            "delisted_sample": [{"code": s.code, "name": s.name, "type": s.security_type} for s in delisted_rows[:5]],
            "fx_field_names_returned": fx.observed_field_names,
            "fx_rows": len(fx.payload) if isinstance(fx.payload, list) else None,
            "quota": provider.quota.as_dict(),
        },
        objects=records,
    )


# ------------------------------------------------------------- object store
def check_object_store(store: ObjectStore) -> CheckResult:
    """Write, read back, and confirm the store refuses to overwrite."""

    payload = json.dumps(
        {"surge": "phase-2.1 smoke", "at": datetime.now(UTC).isoformat()}, sort_keys=True
    ).encode("utf-8")

    try:
        first = store.put_content_addressed("surge", "SMOKE_TEST", payload, "application/json", "json")
        again = store.put_content_addressed("surge", "SMOKE_TEST", payload, "application/json", "json")
        read_back = store.get(first.key)

        conflict_refused = False
        conflict_detail = ""
        try:
            store.put_immutable(first.key, payload + b"\n-- different bytes", "application/json")
        except ImmutableObjectConflict as exc:
            conflict_refused = True
            conflict_detail = str(exc)

        head = store.head(first.key)
        deleted = store.delete(first.key)
        gone = store.head(first.key) is None
    except Exception as exc:  # noqa: BLE001
        return CheckResult("storage.write_once", CheckStatus.FAILED, f"{type(exc).__name__}: {exc}")

    problems = []
    if first.created is not True:
        problems.append("the first write did not report itself as created")
    if again.created is not False:
        problems.append("re-writing identical bytes was not recognised as a no-op")
    if read_back != payload:
        problems.append("the bytes read back are not the bytes written")
    if not conflict_refused:
        problems.append("the store ACCEPTED an overwrite with different bytes")
    if not deleted or not gone:
        problems.append("the purge path could not delete the object")

    return CheckResult(
        "storage.write_once",
        CheckStatus.FAILED if problems else CheckStatus.PASSED,
        "; ".join(problems) or "write-once, idempotent re-write, overwrite refused, delete works",
        evidence={
            "store_id": store.store_id,
            "key": first.key,
            "created_first": first.created,
            "created_again": again.created,
            "bytes": head.bytes if head else None,
            "overwrite_refused": conflict_refused,
            "overwrite_detail": conflict_detail,
            "deleted": deleted,
        },
    )


# ------------------------------------------------------------------- helpers
def _sample(rows: list[Any]) -> dict[str, Any] | None:
    if not rows:
        return None
    row = rows[0]
    return {
        key: (str(value) if value is not None else None)
        for key, value in vars(row).items()
        if key != "raw"
    }


def _previous_business_day(today: date) -> date:
    """The most recent weekday before today.

    A market holiday simply returns no rows, which the check reports rather than
    hides - a calendar is Phase 2.2's problem, not a smoke test's.
    """

    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


CHECKS: dict[str, Callable[[ObjectStore], CheckResult]] = {
    "storage": check_object_store,
    "ecb": check_ecb,
    "openfigi": check_openfigi,
    "jquants": check_jquants,
    "eodhd": check_eodhd,
}


def run(selected: list[str], store: ObjectStore) -> SmokeReport:
    report = SmokeReport(started_at=datetime.now(UTC))
    for name in selected:
        report.add(CHECKS[name](store))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2.1 provider smoke test")
    parser.add_argument(
        "--checks",
        default=",".join(CHECKS),
        help="comma separated subset of: " + ", ".join(CHECKS),
    )
    parser.add_argument(
        "--store",
        default=None,
        help="object store target, e.g. local:./.local/phase21 or r2 (default: SURGE_OBJECT_STORE)",
    )
    parser.add_argument("--out", default=None, help="write the JSON report here as well as to stdout")
    args = parser.parse_args(argv)

    selected = [name.strip() for name in args.checks.split(",") if name.strip()]
    unknown = [name for name in selected if name not in CHECKS]
    if unknown:
        parser.error(f"unknown checks: {', '.join(unknown)}")

    store = open_store(args.store)
    report = run(selected, store)
    payload = json.dumps(report.as_dict(), indent=2, ensure_ascii=False)
    print(payload)
    if args.out:
        from pathlib import Path  # noqa: PLC0415 - only needed when asked to write

        Path(args.out).write_text(payload + "\n", encoding="utf-8")

    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
