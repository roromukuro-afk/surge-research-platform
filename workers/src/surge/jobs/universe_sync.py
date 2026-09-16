"""Security master + universe synchronisation for one market.

The job is deliberately boring: fetch official snapshots, normalise them,
classify each record against a versioned universe definition, and emit the SQL
that loads the snapshot. Every output carries the run id, the source content
hash and the times the data was observed and became usable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from surge import JOB_VERSION
from surge.config import Settings, load_settings
from surge.models import FetchResult, Provenance, RawSecurityRecord, UniverseDecision
from surge.normalize import normalize_name
from surge.providers.jpx_listed import JpxListedIssuesProvider
from surge.providers.nasdaq_trader import NasdaqTraderProvider
from surge.providers.sec_edgar import SIC_BLANK_CHECK, SIC_REIT, SecCompanyTickers, SecSicDirectory
from surge.sql_emit import SNAPSHOT_COLUMNS, Json, insert_statement, quote, tsv_line
from surge.universe import UNIVERSE_VERSION, UniverseContext, classify

JOB_NAME = "universe_sync"


@dataclass
class SyncResult:
    run_id: uuid.UUID
    market_code: str
    as_of_date: date
    universe_version: str
    records: list[RawSecurityRecord]
    decisions: list[UniverseDecision]
    provenances: list[Provenance]
    errors: list[str]
    expected_population: dict[str, int] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def decision_counts(self) -> dict[str, int]:
        return dict(Counter(decision.decision for decision in self.decisions))

    @property
    def reason_counts(self) -> dict[str, int]:
        return dict(Counter(decision.reason_code for decision in self.decisions))

    @property
    def duplicate_count(self) -> int:
        keys = [(record.exchange_id, record.local_code) for record in self.records]
        return len(keys) - len(set(keys))


def _sources_data_version(provenances: list[Provenance]) -> str:
    """One short, stable fingerprint of every source snapshot used by the run."""

    parts = "|".join(f"{p.source_id}:{p.content_sha256}" for p in provenances if p.content_sha256)
    return hashlib.sha256(parts.encode()).hexdigest()[:24] if parts else "unversioned"


def run_universe_sync(
    market_code: str,
    *,
    settings: Settings | None = None,
    as_of: date | None = None,
    sic_max_pages: int = 60,
) -> SyncResult:
    settings = settings or load_settings()
    as_of = as_of or datetime.now(UTC).date()
    run_id = uuid.uuid4()
    errors: list[str] = []
    provenances: list[Provenance] = []
    notes: dict[str, Any] = {}

    if market_code == "JP":
        fetched: FetchResult = JpxListedIssuesProvider(user_agent=settings.user_agent).fetch_security_master()
        records = list(fetched.records)
        provenances.append(fetched.provenance)
        errors.extend(fetched.errors)
        context = UniverseContext()
        expected = {f"MARKET:{market_code}": fetched.provenance.item_count}

    elif market_code == "US":
        fetched = NasdaqTraderProvider(user_agent=settings.user_agent).fetch_security_master()
        records = list(fetched.records)
        provenances.append(fetched.provenance)
        errors.extend(fetched.errors)

        cik_map, cik_provenance = SecCompanyTickers(user_agent=settings.user_agent).fetch_map()
        provenances.append(cik_provenance)

        records = [
            record
            if record.symbol is None or record.symbol not in cik_map
            else _with_cik(record, cik_map[record.symbol].cik)
            for record in records
        ]

        sic_directory = SecSicDirectory(user_agent=settings.user_agent)
        spac = sic_directory.fetch_membership(SIC_BLANK_CHECK, max_pages=sic_max_pages)
        reit = sic_directory.fetch_membership(SIC_REIT, max_pages=sic_max_pages)
        provenances.extend([spac.provenance, reit.provenance])
        notes["sic_blank_check"] = {
            "ciks": len(spac.ciks),
            "pages": spac.pages_fetched,
            "truncated": spac.truncated,
        }
        notes["sic_reit"] = {"ciks": len(reit.ciks), "pages": reit.pages_fetched, "truncated": reit.truncated}
        if spac.truncated or reit.truncated:
            errors.append("SEC SIC listing truncated at max_pages; SPAC/REIT detection may be incomplete")

        context = UniverseContext(spac_ciks=spac.ciks, reit_ciks=reit.ciks, sic_lookup_available=True)
        expected = {f"MARKET:{market_code}": fetched.provenance.item_count}

    else:
        raise ValueError(f"unsupported market: {market_code}")

    decisions = [classify(record, context) for record in records]

    return SyncResult(
        run_id=run_id,
        market_code=market_code,
        as_of_date=as_of,
        universe_version=UNIVERSE_VERSION,
        records=records,
        decisions=decisions,
        provenances=provenances,
        errors=errors,
        expected_population=expected,
        notes=notes,
    )


def _with_cik(record: RawSecurityRecord, cik: str) -> RawSecurityRecord:
    return RawSecurityRecord(
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        market_code=record.market_code,
        exchange_id=record.exchange_id,
        local_code=record.local_code,
        name=record.name,
        security_type=record.security_type,
        symbol=record.symbol,
        market_segment_code=record.market_segment_code,
        market_segment_name=record.market_segment_name,
        currency=record.currency,
        country=record.country,
        is_adr=record.is_adr,
        is_test_issue=record.is_test_issue,
        listing_status=record.listing_status,
        cik=cik,
        type_evidence=record.type_evidence,
    )


def _spac_flag(record: RawSecurityRecord, decision: UniverseDecision) -> bool | None:
    """True / False only when the SIC check could actually run; otherwise unknown."""

    if decision.reason_code == "SPAC_PRE_MERGER":
        return True
    if record.market_code == "US" and record.cik:
        return False
    return None


def snapshot_rows(result: SyncResult) -> list[list[Any]]:
    version = _sources_data_version(result.provenances)
    primary = result.provenances[0]
    rows: list[list[Any]] = []
    for record, decision in zip(result.records, result.decisions, strict=True):
        rows.append(
            [
                str(result.run_id),
                record.source_id,
                record.source_record_id,
                record.market_code,
                record.exchange_id,
                record.local_code,
                record.symbol,
                record.name,
                normalize_name(record.name),
                record.security_type,
                record.market_segment_code,
                record.market_segment_name,
                record.currency,
                record.country,
                record.is_adr,
                record.is_test_issue,
                _spac_flag(record, decision),
                record.listing_status,
                record.cik,
                Json(record.type_evidence),
                decision.decision,
                decision.reason_code,
                # Detail explains the non-obvious outcomes; an included record is
                # already fully described by its own columns.
                Json(decision.detail if decision.decision != "INCLUDED" else {}),
                primary.observed_at,
                primary.available_at,
                version,
            ]
        )
    return rows


def write_sql_artifacts(result: SyncResult, out_dir: Path, *, git_sha: str | None = None) -> list[Path]:
    """Write run, provenance, snapshot and apply statements as .sql files."""

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    version = _sources_data_version(result.provenances)

    run_sql = insert_statement(
        "pipeline.runs",
        (
            "run_id",
            "job_name",
            "job_version",
            "run_mode",
            "market_code",
            "idempotency_key",
            "status",
            "as_of_date",
            "data_cutoff",
            "git_sha",
            "versions",
            "provider_bindings",
            "params",
            "runner_id",
        ),
        [
            [
                str(result.run_id),
                JOB_NAME,
                JOB_VERSION,
                "PRODUCTION",
                result.market_code,
                f"{JOB_NAME}:{result.market_code}:{result.as_of_date.isoformat()}:{result.run_id}",
                "RUNNING",
                result.as_of_date,
                result.provenances[0].observed_at,
                git_sha,
                Json({"universe_version": result.universe_version, "job_version": JOB_VERSION}),
                Json({provenance.source_id: provenance.endpoint for provenance in result.provenances}),
                Json({"source_data_version": version, "notes": result.notes}),
                "local-cli",
            ]
        ],
    )

    fetch_rows = [
        [
            str(result.run_id),
            provenance.source_id,
            provenance.endpoint,
            provenance.requested_at,
            provenance.received_at,
            provenance.http_status,
            provenance.item_count,
            provenance.bytes,
            provenance.content_sha256 or None,
            provenance.observed_at,
            provenance.available_at,
        ]
        for provenance in result.provenances
    ]
    fetch_sql = insert_statement(
        "pipeline.source_fetches",
        (
            "run_id",
            "source_id",
            "endpoint",
            "requested_at",
            "received_at",
            "http_status",
            "item_count",
            "bytes",
            "content_sha256",
            "observed_at",
            "available_at",
        ),
        fetch_rows,
    )

    error_sql = insert_statement(
        "pipeline.run_errors",
        ("run_id", "stage", "severity", "error_type", "message"),
        [[str(result.run_id), "provider_fetch", "WARNING", "PROVIDER_DATA", message] for message in result.errors],
    )

    header = out_dir / f"{result.market_code.lower()}_00_run.sql"
    header.write_text("\n\n".join(part for part in (run_sql, fetch_sql, error_sql) if part) + "\n", encoding="utf-8")
    written.append(header)

    # The snapshot itself goes out as a delimited file for object storage, so the
    # bulk data never has to pass through the orchestration layer.
    rows = snapshot_rows(result)
    snapshot_path = out_dir / f"{result.market_code.lower()}_snapshot.tsv"
    run_id_index = SNAPSHOT_COLUMNS.index("run_id")
    with snapshot_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = [value for index, value in enumerate(row) if index != run_id_index]
            handle.write(tsv_line(payload) + "\n")
    written.append(snapshot_path)

    expected_json = json.dumps(result.expected_population, ensure_ascii=False, sort_keys=True)
    apply_sql = "\n".join(
        [
            "-- Load the snapshot first, for example:",
            (
                "--   select pipeline.load_master_snapshot_from_url("
                f"{quote(str(result.run_id))}::uuid, '<object storage url>', '<publishable key>');"
            ),
            f"select ref.apply_master_snapshot({quote(str(result.run_id))}::uuid) as master_result;",
            (
                "select universe.apply_snapshot_evaluations("
                f"{quote(str(result.run_id))}::uuid, {quote(result.universe_version)}, "
                f"{quote(result.as_of_date)}::date) as evaluations_inserted;"
            ),
            (
                "select universe.compute_coverage("
                f"{quote(str(result.run_id))}::uuid, {quote(result.universe_version)}, "
                f"{quote(result.as_of_date)}::date, {quote(expected_json)}::jsonb, "
                f"{quote('provider_file_row_count')}) as coverage_rows;"
            ),
            (
                "update pipeline.runs set status = 'SUCCEEDED', finished_at = now() "
                f"where run_id = {quote(str(result.run_id))}::uuid;"
            ),
        ]
    )
    apply_path = out_dir / f"{result.market_code.lower()}_99_apply.sql"
    apply_path.write_text(apply_sql + "\n", encoding="utf-8")
    written.append(apply_path)

    summary = {
        "run_id": str(result.run_id),
        "market_code": result.market_code,
        "as_of_date": result.as_of_date.isoformat(),
        "universe_version": result.universe_version,
        "retrieved_count": len(result.records),
        "unique_count": len({(record.exchange_id, record.local_code) for record in result.records}),
        "duplicate_count": result.duplicate_count,
        "decisions": result.decision_counts,
        "reasons": result.reason_counts,
        "provider_errors": len(result.errors),
        "sources": [
            {
                "source_id": provenance.source_id,
                "endpoint": provenance.endpoint,
                "http_status": provenance.http_status,
                "item_count": provenance.item_count,
                "bytes": provenance.bytes,
                "content_sha256": provenance.content_sha256,
                "observed_at": provenance.observed_at.isoformat(),
                "available_at": provenance.available_at.isoformat(),
            }
            for provenance in result.provenances
        ],
        "notes": result.notes,
        "source_data_version": version,
    }
    summary_path = out_dir / f"{result.market_code.lower()}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(summary_path)

    return written
