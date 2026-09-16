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
