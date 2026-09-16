"""Security master + universe synchronisation for one market.

The job fetches official snapshots, resolves issuer and security identity from
stable registry identifiers, classifies every record against a versioned
universe definition, and emits the snapshot for bulk loading. Every output
carries the run id, the source content hash and the times the data was observed
and became usable.

Identity is never derived from a ticker: see surge.identity.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from surge import JOB_VERSION
from surge.config import Settings, load_settings
from surge.identity import (
    IDENTITY_VERSION,
    Identity,
    IdentityCollision,
    demote_colliding_identities,
    issuer_identity,
    security_identity,
)
from surge.models import (
    ERROR_DATA_QUALITY,
    ERROR_IDENTITY_COLLISION,
    ERROR_PROVIDER_DATA,
    FetchResult,
    Provenance,
    RawSecurityRecord,
    RunError,
    UniverseDecision,
)
from surge.normalize import normalize_name
from surge.providers.edinet import EdinetCodeList, jpx_code_to_securities_code
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
    errors: list[RunError]
    issuer_identities: list[Identity] = field(default_factory=list)
    security_identities: list[Identity] = field(default_factory=list)
    expected_population: dict[str, int] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    run_mode: str = "PRODUCTION"
    # Everything that can change the result and is not the provider payload
    # itself. Hashed into config_hash and recorded with the run.
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def decision_counts(self) -> dict[str, int]:
        return dict(Counter(decision.decision for decision in self.decisions))

    @property
    def reason_counts(self) -> dict[str, int]:
        return dict(Counter(decision.reason_code for decision in self.decisions))

    @property
    def identity_counts(self) -> dict[str, int]:
        return dict(Counter(identity.source for identity in self.security_identities))

    @property
    def issuer_identity_counts(self) -> dict[str, int]:
        return dict(Counter(identity.source for identity in self.issuer_identities))

    @property
    def duplicate_count(self) -> int:
        keys = [(record.exchange_id, record.local_code) for record in self.records]
        return len(keys) - len(set(keys))

    # Coverage reports these separately: a provider that failed is not the same
    # thing as an identity that could not be proven unique.
    @property
    def provider_error_count(self) -> int:
        return sum(1 for error in self.errors if error.error_type == ERROR_PROVIDER_DATA)

    @property
    def data_quality_warning_count(self) -> int:
        return sum(1 for error in self.errors if error.error_type == ERROR_DATA_QUALITY)

    @property
    def identity_collision_record_count(self) -> int:
        return sum(1 for error in self.errors if error.error_type == ERROR_IDENTITY_COLLISION)

    @property
    def identity_collision_key_count(self) -> int:
        return len(
            {
                error.context.get("identity_key")
                for error in self.errors
                if error.error_type == ERROR_IDENTITY_COLLISION
            }
        )


def canonical_config_hash(config: dict[str, Any]) -> str:
    """Stable fingerprint of the settings that can change a run's output."""

    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key(
    *,
    market_code: str,
    as_of: date,
    run_mode: str,
    source_data_version: str,
    universe_version: str,
    identity_version: str,
    job_version: str,
    config_hash: str,
) -> str:
    """The logical invocation, not the attempt.

    Re-running the same job over the same provider snapshot with the same code
    and configuration is the SAME invocation, so it produces the same key and a
    retry cannot create a second run. A new provider snapshot, a new universe or
    identity version, new code or changed configuration is a different
    invocation and gets its own key. The run id is deliberately not a component.
    """

    material = "|".join(
        [
            JOB_NAME,
            run_mode,
            market_code,
            as_of.isoformat(),
            source_data_version,
            universe_version,
            identity_version,
            job_version,
            config_hash,
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{JOB_NAME}:{run_mode}:{market_code}:{as_of.isoformat()}:{digest}"


def _sources_data_version(provenances: list[Provenance]) -> str:
    """One short, stable fingerprint of every source snapshot used by the run."""

    parts = "|".join(f"{p.source_id}:{p.content_sha256}" for p in provenances if p.content_sha256)
    return hashlib.sha256(parts.encode()).hexdigest()[:24] if parts else "unversioned"


def _provider_errors(messages: list[str] | tuple[str, ...]) -> list[RunError]:
    """Provider level failures: a file that would not parse, a code we cannot map."""

    return [RunError(error_type=ERROR_PROVIDER_DATA, message=message) for message in messages]


def resolve_identities(
    records: list[RawSecurityRecord],
) -> tuple[list[Identity], list[Identity], list[IdentityCollision]]:
    """Issuer and security identity for each record, with collision fallback.

    Securities are resolved first, because an issuer without a registry
    identifier is keyed on its security's coordinate rather than on a name: a
    name that two companies happen to share must not merge them.
    """

    securities = [
        security_identity(
            market_code=record.market_code,
            exchange_id=record.exchange_id,
            local_code=record.local_code,
            symbol=record.symbol,
            name=record.name,
            security_type=record.security_type,
            cik=record.cik,
        )
        for record in records
    ]
    securities, collisions = demote_colliding_identities(
        securities,
        exchange_ids=[record.exchange_id for record in records],
        symbols=[record.symbol for record in records],
        local_codes=[record.local_code for record in records],
        market_codes=[record.market_code for record in records],
    )
    issuers = [
        issuer_identity(
            market_code=record.market_code,
            security_identity_key=security.key,
            cik=record.cik,
            edinet_code=record.edinet_code,
        )
        for record, security in zip(records, securities, strict=True)
    ]
    return issuers, securities, collisions


def run_universe_sync(
    market_code: str,
    *,
    settings: Settings | None = None,
    as_of: date | None = None,
    sic_max_pages: int = 60,
    run_mode: str = "PRODUCTION",
) -> SyncResult:
    settings = settings or load_settings()
    as_of = as_of or datetime.now(UTC).date()
    run_id = uuid.uuid4()
    errors: list[RunError] = []
    provenances: list[Provenance] = []
    notes: dict[str, Any] = {}

    if market_code == "JP":
        fetched: FetchResult = JpxListedIssuesProvider(user_agent=settings.user_agent).fetch_security_master()
        records = list(fetched.records)
        provenances.append(fetched.provenance)
        errors.extend(_provider_errors(fetched.errors))

        edinet_map, edinet_provenance = EdinetCodeList(user_agent=settings.user_agent).fetch_map()
        provenances.append(edinet_provenance)
        matched = 0
        enriched: list[RawSecurityRecord] = []
        for record in records:
            issuer = edinet_map.get(jpx_code_to_securities_code(record.local_code))
            if issuer is None:
                enriched.append(record)
                continue
            matched += 1
            # 提出者名 is the filing entity's own name. The JPX workbook only
            # carries the security display name, which is not the same thing.
            enriched.append(
                replace(
                    record,
                    edinet_code=issuer.edinet_code,
                    corporate_number=issuer.corporate_number,
                    issuer_name=issuer.name or None,
                    issuer_name_source=EdinetCodeList.provider_id if issuer.name else None,
                )
            )
        records = enriched
        notes["edinet"] = {
            "code_list_size": len(edinet_map),
            "matched": matched,
            "unmatched": len(records) - matched,
        }
        context = UniverseContext()
        expected = {f"MARKET:{market_code}": fetched.provenance.item_count}

    elif market_code == "US":
        fetched = NasdaqTraderProvider(user_agent=settings.user_agent).fetch_security_master()
        records = list(fetched.records)
        provenances.append(fetched.provenance)
        errors.extend(_provider_errors(fetched.errors))

        cik_map, cik_provenance = SecCompanyTickers(user_agent=settings.user_agent).fetch_map()
        provenances.append(cik_provenance)
        # The SEC file carries the EDGAR registrant name next to the CIK; the
        # Nasdaq directory only has the product name ("... - Common Stock").
        records = [
            replace(
                record,
                cik=cik_map[record.symbol].cik,
                issuer_name=cik_map[record.symbol].name or None,
                issuer_name_source=SecCompanyTickers.provider_id if cik_map[record.symbol].name else None,
            )
            if record.symbol and record.symbol in cik_map
            else record
            for record in records
        ]
        notes["cik"] = {
            "map_size": len(cik_map),
            "matched": sum(1 for record in records if record.cik),
            "unmatched": sum(1 for record in records if not record.cik),
        }

        sic_directory = SecSicDirectory(user_agent=settings.user_agent)
        spac = sic_directory.fetch_membership(SIC_BLANK_CHECK, max_pages=sic_max_pages)
        reit = sic_directory.fetch_membership(SIC_REIT, max_pages=sic_max_pages)
        provenances.extend([spac.provenance, reit.provenance])
        notes["sic_blank_check"] = {"ciks": len(spac.ciks), "pages": spac.pages_fetched, "truncated": spac.truncated}
        notes["sic_reit"] = {"ciks": len(reit.ciks), "pages": reit.pages_fetched, "truncated": reit.truncated}
        if spac.truncated or reit.truncated:
            errors.append(
                RunError(
                    error_type=ERROR_DATA_QUALITY,
                    message="SEC SIC listing truncated at max_pages; SPAC/REIT detection may be incomplete",
                    context={"sic_blank_check_truncated": spac.truncated, "sic_reit_truncated": reit.truncated},
                    stage="classify",
                )
            )

        context = UniverseContext(spac_ciks=spac.ciks, reit_ciks=reit.ciks, sic_lookup_available=True)
        expected = {f"MARKET:{market_code}": fetched.provenance.item_count}

    else:
        raise ValueError(f"unsupported market: {market_code}")

    issuer_ids, security_ids, collisions = resolve_identities(records)
    errors.extend(
        RunError(
            error_type=ERROR_IDENTITY_COLLISION,
            message=collision.message,
            context=collision.context,
            stage="resolve_identity",
        )
        for collision in collisions
    )
    decisions = [classify(record, context) for record in records]

    notes["identity"] = {
        "version": IDENTITY_VERSION,
        "security": dict(Counter(identity.source for identity in security_ids)),
        "issuer": dict(Counter(identity.source for identity in issuer_ids)),
        "collision_records": len(collisions),
        "collision_keys": len({collision.identity_key for collision in collisions}),
        "issuer_names_from_registry": sum(1 for record in records if record.issuer_name),
    }

    config = {
        "job_version": JOB_VERSION,
        "universe_version": UNIVERSE_VERSION,
        "identity_version": IDENTITY_VERSION,
        "market_code": market_code,
        "sic_max_pages": sic_max_pages if market_code == "US" else None,
        "sources": {
            provenance.source_id: provenance.endpoint for provenance in provenances
        },
    }

    return SyncResult(
        run_id=run_id,
        market_code=market_code,
        as_of_date=as_of,
        universe_version=UNIVERSE_VERSION,
        run_mode=run_mode,
        config=config,
        records=records,
        decisions=decisions,
        provenances=provenances,
        errors=errors,
        issuer_identities=issuer_ids,
        security_identities=security_ids,
        expected_population=expected,
        notes=notes,
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
    expected_width = len(SNAPSHOT_COLUMNS)
    for record, decision, issuer, security in zip(
        result.records, result.decisions, result.issuer_identities, result.security_identities, strict=True
    ):
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
                record.edinet_code,
                record.corporate_number,
                issuer.source,
                issuer.key,
                issuer.confidence,
                security.source,
                security.key,
                security.confidence,
                Json(record.type_evidence),
                decision.decision,
                decision.reason_code,
                # Detail explains the non-obvious outcomes; an included record is
                # already fully described by its own columns.
                Json(decision.detail if decision.decision != "INCLUDED" else {}),
                primary.observed_at,
                primary.available_at,
                version,
                record.issuer_name,
                record.issuer_name_source,
                IDENTITY_VERSION,
                # normalised from the ISSUER's registry name, so the issuer's
                # matching key is not a normalised product name
                normalize_name(record.issuer_name) if record.issuer_name else None,
            ]
        )
        # The SQL loader reads the file positionally and silently drops rows of
        # the wrong width, so a mismatch has to fail here instead.
        if len(rows[-1]) != expected_width:
            raise ValueError(f"snapshot row has {len(rows[-1])} fields, expected {expected_width}")
    return rows


def write_sql_artifacts(result: SyncResult, out_dir: Path, *, git_sha: str | None = None) -> list[Path]:
    """Write run, provenance, snapshot and apply statements."""

    version = _sources_data_version(result.provenances)
    config_hash = canonical_config_hash(result.config)

    # A production run has to be attributable to a commit and a configuration.
    # Without them a result cannot be reproduced or re-audited later, so the
    # artefact is refused rather than written with NULL provenance.
    if result.run_mode == "PRODUCTION" and not git_sha:
        raise ValueError(
            "a PRODUCTION run needs --git-sha: refusing to write an artefact that cannot be "
            "attributed to a commit (use --run-mode DEV for local experiments)"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

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
            "config_hash",
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
                result.run_mode,
                result.market_code,
                idempotency_key(
                    market_code=result.market_code,
                    as_of=result.as_of_date,
                    run_mode=result.run_mode,
                    source_data_version=version,
                    universe_version=result.universe_version,
                    identity_version=IDENTITY_VERSION,
                    job_version=JOB_VERSION,
                    config_hash=config_hash,
                ),
                "RUNNING",
                result.as_of_date,
                result.provenances[0].observed_at,
                git_sha,
                config_hash,
                Json(
                    {
                        "universe_version": result.universe_version,
                        "identity_version": IDENTITY_VERSION,
                        "job_version": JOB_VERSION,
                    }
                ),
                Json({provenance.source_id: provenance.endpoint for provenance in result.provenances}),
                Json({"source_data_version": version, "config": result.config, "notes": result.notes}),
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
        ("run_id", "stage", "severity", "error_type", "message", "context"),
        [
            [
                str(result.run_id),
                error.stage,
                error.severity,
                error.error_type,
                error.message,
                Json(error.context),
            ]
            for error in result.errors
        ],
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
                "--   select pipeline.load_master_snapshot_from_signed_url("
                f"{quote(str(result.run_id))}::uuid, '<signed object storage url>');"
            ),
            (
                "select ref.apply_master_snapshot_with_name_refresh("
                f"{quote(str(result.run_id))}::uuid) as master_result;"
            ),
            (
                "select universe.apply_snapshot_evaluations("
                f"{quote(str(result.run_id))}::uuid, {quote(result.universe_version)}, "
                f"{quote(result.as_of_date)}::date) as evaluations_inserted;"
            ),
            (
                "select universe.compute_coverage("
                f"{quote(str(result.run_id))}::uuid, {quote(result.universe_version)}, "
                f"{quote(result.as_of_date)}::date, {quote(expected_json)}::jsonb, "
                f"{quote('provider_parsed_record_count')}) as coverage_rows;"
            ),
            (
                "-- coverage splits the run diagnostics by kind: expected here are "
                f"{result.provider_error_count} provider errors, "
                f"{result.data_quality_warning_count} data quality warnings, "
                f"{result.identity_collision_record_count} identity collision records "
                f"over {result.identity_collision_key_count} keys."
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
        "run_mode": result.run_mode,
        "job_version": JOB_VERSION,
        "git_sha": git_sha,
        "config_hash": config_hash,
        "universe_version": result.universe_version,
        "retrieved_count": len(result.records),
        "unique_count": len({(record.exchange_id, record.local_code) for record in result.records}),
        "duplicate_count": result.duplicate_count,
        "decisions": result.decision_counts,
        "reasons": result.reason_counts,
        "security_identity_sources": result.identity_counts,
        "issuer_identity_sources": result.issuer_identity_counts,
        # Split, not one "errors" number: 246 provider errors and 6 provider
        # errors plus 240 identity warnings are very different reports.
        "provider_errors": result.provider_error_count,
        "data_quality_warnings": result.data_quality_warning_count,
        "identity_collision_records": result.identity_collision_record_count,
        "identity_collision_keys": result.identity_collision_key_count,
        "identity_version": IDENTITY_VERSION,
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
