"""Phase 1.1b: a production run is attributable, and its key is its identity.

An idempotency key that contains the run id is not an idempotency key: every
attempt looks like a new invocation. These fix what the key means.
"""

from __future__ import annotations

from datetime import date

import pytest

from surge import JOB_VERSION
from surge.jobs.universe_sync import canonical_config_hash, idempotency_key

BASE = {
    "market_code": "JP",
    "as_of": date(2026, 9, 16),
    "run_mode": "PRODUCTION",
    "source_data_version": "abc123",
    "universe_version": "universe-1.0.0",
    "identity_version": "identity-1.1a",
    "job_version": JOB_VERSION,
    "config_hash": "cfg0",
}


def test_same_logical_invocation_has_the_same_key():
    assert idempotency_key(**BASE) == idempotency_key(**BASE)


def test_the_run_id_is_not_part_of_the_key():
    """Two attempts at the same work are the same invocation, not two runs."""

    key = idempotency_key(**BASE)
    assert "uuid" not in key
    # a fresh call, as a retry would make, lands on the same key
    assert idempotency_key(**BASE) == key


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_data_version", "def456"),   # the provider snapshot moved
        ("universe_version", "universe-1.1.0"),
        ("identity_version", "identity-2.0.0"),
        ("job_version", "universe_sync-9.9.9"),
        ("config_hash", "cfg1"),             # configuration changed
        ("market_code", "US"),
        ("run_mode", "RESEARCH"),
    ],
)
def test_a_different_invocation_gets_a_different_key(field, value):
    other = {**BASE, field: value}
    assert idempotency_key(**other) != idempotency_key(**BASE)


def test_a_new_as_of_date_is_a_new_invocation():
    other = {**BASE, "as_of": date(2026, 9, 17)}
    assert idempotency_key(**other) != idempotency_key(**BASE)


def test_the_key_is_readable_before_it_is_unique():
    key = idempotency_key(**BASE)
    assert key.startswith("universe_sync:PRODUCTION:JP:2026-09-16:")


def test_config_hash_is_order_independent_and_change_sensitive():
    a = canonical_config_hash({"sic_max_pages": 60, "sources": {"x": "1", "y": "2"}})
    b = canonical_config_hash({"sources": {"y": "2", "x": "1"}, "sic_max_pages": 60})
    c = canonical_config_hash({"sic_max_pages": 40, "sources": {"x": "1", "y": "2"}})
    assert a == b
    assert a != c


def test_a_production_artefact_without_a_commit_is_refused(tmp_path):
    """Provenance is not optional: an unattributable run is not written at all."""

    from datetime import UTC, datetime

    from surge.jobs.universe_sync import SyncResult, write_sql_artifacts
    from surge.models import Provenance

    now = datetime.now(UTC)
    provenance = Provenance(
        source_id="fixture", endpoint="fixture://", requested_at=now, received_at=now,
        http_status=200, bytes=0, content_sha256="abc", item_count=0,
        observed_at=now, available_at=now,
    )
    result = SyncResult(
        run_id=__import__("uuid").uuid4(),
        market_code="JP",
        as_of_date=now.date(),
        universe_version="universe-1.0.0",
        records=[],
        decisions=[],
        provenances=[provenance],
        errors=[],
        run_mode="PRODUCTION",
        config={"job_version": JOB_VERSION},
    )

    with pytest.raises(ValueError, match="git-sha"):
        write_sql_artifacts(result, tmp_path)

    # the same run is fine as an explicitly non-production run
    result.run_mode = "DEV"
    written = write_sql_artifacts(result, tmp_path)
    assert written


# ----------------------------------------- Phase 1.1c: temporal provenance
def test_data_cutoff_is_the_last_source_not_the_first():
    """A run is not usable until everything it depends on has arrived."""

    from datetime import UTC, datetime, timedelta

    from surge.jobs.universe_sync import dependency_available_at, first_source_available_at
    from surge.models import Provenance

    base = datetime(2026, 9, 16, 7, 23, 41, tzinfo=UTC)

    def at(seconds: int, source: str) -> Provenance:
        moment = base + timedelta(seconds=seconds)
        return Provenance(
            source_id=source, endpoint="fixture://", requested_at=moment, received_at=moment,
            http_status=200, bytes=1, content_sha256="a" * 64, item_count=1,
            observed_at=moment, available_at=moment,
        )

    # the real shape of a US run: Nasdaq, then the SEC ticker file, then two SIC pages
    provenances = [
        at(0, "nasdaq_trader_symbol_directory"),
        at(1, "sec_company_tickers"),
        at(32, "sec_sic_directory"),
        at(42, "sec_sic_directory"),
    ]

    assert dependency_available_at(provenances) == base + timedelta(seconds=42)
    assert first_source_available_at(provenances) == base
    # the primary provider's time is NOT the cutoff
    assert dependency_available_at(provenances) != provenances[0].available_at


def test_snapshot_rows_use_the_dependency_cutoff(tmp_path):
    """Every row's available_at is the last source, not the primary provider's."""

    import uuid as uuid_module
    from datetime import UTC, datetime, timedelta

    from surge.jobs.universe_sync import SyncResult, snapshot_rows
    from surge.models import Provenance, RawSecurityRecord, UniverseDecision
    from surge.sql_emit import SNAPSHOT_COLUMNS

    base = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)
    late = base + timedelta(minutes=5)

    def provenance(source: str, moment: datetime) -> Provenance:
        return Provenance(
            source_id=source, endpoint="fixture://", requested_at=moment, received_at=moment,
            http_status=200, bytes=1, content_sha256="b" * 64, item_count=1,
            observed_at=moment, available_at=moment,
        )

    record = RawSecurityRecord(
        source_id="jpx_listed_issues", source_record_id="1301", market_code="JP",
        exchange_id="XTKS", local_code="1301", symbol="1301", name="極洋",
        security_type="COMMON_STOCK", currency="JPY", country="JP",
    )
    from surge.jobs.universe_sync import resolve_identities

    issuers, securities, _ = resolve_identities([record])
    result = SyncResult(
        run_id=uuid_module.uuid4(), market_code="JP", as_of_date=base.date(),
        universe_version="universe-1.0.0",
        records=[record],
        decisions=[UniverseDecision("INCLUDED", "TARGET_MARKET_COMMON_STOCK")],
        provenances=[provenance("jpx_listed_issues", base), provenance("edinet_code_list", late)],
        errors=[], run_mode="DEV",
        issuer_identities=issuers, security_identities=securities,
    )

    row = snapshot_rows(result)[0]
    observed_at = row[SNAPSHOT_COLUMNS.index("observed_at")]
    available_at = row[SNAPSHOT_COLUMNS.index("available_at")]

    assert observed_at == base, "observed_at stays the primary payload's read time"
    assert available_at == late, "available_at waits for the enrichment source"


# ------------------------------------------- Phase 2.0: bindings by dataset
def test_provider_bindings_are_keyed_by_dataset_not_by_provider():
    """One provider serving two required datasets must not collapse into one."""

    from datetime import UTC, datetime, timedelta

    from surge.jobs.universe_sync import provider_bindings
    from surge.models import Provenance
    from surge.providers.sec_edgar import sic_dataset_key

    base = datetime(2026, 9, 16, 7, 23, 41, tzinfo=UTC)

    def fetch(seconds: int, source: str, endpoint: str, dataset_key: str | None = None) -> Provenance:
        moment = base + timedelta(seconds=seconds)
        return Provenance(
            source_id=source, endpoint=endpoint, requested_at=moment, received_at=moment,
            http_status=200, bytes=1, content_sha256="a" * 64, item_count=1,
            observed_at=moment, available_at=moment, dataset_key=dataset_key,
        )

    provenances = [
        fetch(0, "nasdaq_trader_symbol_directory", "ftp://nasdaqtrader"),
        fetch(1, "sec_company_tickers", "https://sec.gov/tickers.json"),
        fetch(32, "sec_sic_directory", "https://sec.gov/browse-edgar?SIC=6770", sic_dataset_key("6770")),
        fetch(42, "sec_sic_directory", "https://sec.gov/browse-edgar?SIC=6798", sic_dataset_key("6798")),
    ]

    bindings = provider_bindings(provenances)

    assert set(bindings) == {
        "nasdaq_trader_symbol_directory",
        "sec_company_tickers",
        "SEC_SIC_6770",
        "SEC_SIC_6798",
    }
    # both SIC datasets keep their own endpoint, and both name the same provider
    assert bindings["SEC_SIC_6770"]["provider"] == "sec_sic_directory"
    assert bindings["SEC_SIC_6798"]["provider"] == "sec_sic_directory"
    assert bindings["SEC_SIC_6770"]["endpoints"] != bindings["SEC_SIC_6798"]["endpoints"]


def test_a_fetch_without_a_dataset_key_is_its_own_dataset():
    from datetime import UTC, datetime

    from surge.models import Provenance

    moment = datetime(2026, 9, 16, 7, 23, 41, tzinfo=UTC)
    plain = Provenance(
        source_id="jpx_listed_issues", endpoint="fixture://", requested_at=moment, received_at=moment,
        http_status=200, bytes=1, content_sha256="a" * 64, item_count=1,
        observed_at=moment, available_at=moment,
    )
    assert plain.dataset == "jpx_listed_issues"
