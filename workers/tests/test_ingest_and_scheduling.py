"""Ingestion, Parquet, retries, revision detection and the scheduler.

No provider credentials are involved. The fetcher is a protocol, so the whole
pipeline is exercised with a fake that can be told to fail, to change its bytes,
or to return nothing - which is exactly the set of behaviours a real provider
will eventually produce and which are hard to arrange on demand against a live
API.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from surge.licensing import AvailabilityBasis, LicenseViolation, Permission, PersistenceDecision
from surge.market.ingest import (
    EMPTY_RESULT,
    PERMANENT_ERROR,
    TRANSIENT_ERROR,
    FetchedDay,
    MarketIngestJob,
    compute_coverage,
    idempotency_key,
)
from surge.market.models import CanonicalBar, NormalisationResult, PriceBasis
from surge.market.parquet_store import ParquetSeriesStore, partition_prefix
from surge.models import Provenance
from surge.scheduling import (
    JOB_DEFINITIONS,
    CalendarScheduler,
    JobDefinition,
    JobStatus,
    LocalJobRunner,
    RunMode,
    build_idempotency_key,
    config_hash,
    next_due,
    trading_days,
)
from surge.storage.local import LocalObjectStore

pyarrow = pytest.importorskip("pyarrow")

T0 = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
PROVIDER = "testfeed"
DATASET = "ECB_EXR_DAILY"  # any dataset with a licence policy recorded
MARKET = "JP"


def _bar(day: date, symbol: str, close: str) -> CanonicalBar:
    price = Decimal(close)
    return CanonicalBar(
        provider_id=PROVIDER, dataset_key=DATASET, market_code=MARKET, native_symbol=symbol,
        trade_date=day, currency="JPY",
        open=price, high=price, low=price, close=price, volume=Decimal(1000), turnover=Decimal(100000),
        open_basis=PriceBasis.RAW, high_basis=PriceBasis.RAW, low_basis=PriceBasis.RAW,
        close_basis=PriceBasis.RAW, volume_basis=PriceBasis.RAW,
        observed_at=T0, available_at=T0, availability_basis=AvailabilityBasis.OBSERVED_NOW,
    )


class FakeFetcher:
    provider_id = PROVIDER
    dataset_key = DATASET
    market_code = MARKET

    def __init__(self, *, fail_times: int = 0, error: Exception | None = None, empty: bool = False) -> None:
        self.calls: list[date] = []
        self._fail_times = fail_times
        self._error = error or TimeoutError("connection timed out")
        self._empty = empty
        self.body_suffix = b""

    def fetch_day(self, trade_date: date) -> FetchedDay:
        self.calls.append(trade_date)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._error

        bars = [] if self._empty else [_bar(trade_date, "13010", "1000"), _bar(trade_date, "13020", "2000")]
        body = f"payload for {trade_date.isoformat()}".encode() + self.body_suffix
        provenance = Provenance(
            source_id=PROVIDER, dataset_key=DATASET, endpoint=f"https://example.invalid/{trade_date}",
            requested_at=T0, received_at=T0, http_status=200, bytes=len(body),
            content_sha256="0" * 64, item_count=len(bars), observed_at=T0, available_at=T0,
        )
        return FetchedDay(
            raw_body=body, provenance=provenance,
            normalised=NormalisationResult(bars=bars),
            entitlement_plan="public", license_policy_version="ecb-2026-09-16",
        )


class FakeWriter:
    """An in-memory stand-in for the database side."""

    def __init__(self) -> None:
        self.raw_objects: list = []
        self.digests: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.revisions: list[dict] = []
        self.bars_written = 0
        self.coverage_written = 0

    def record_raw_object(self, record) -> None:
        self.raw_objects.append(record)

    def last_digest(self, provider_id, dataset_key, natural_key):
        return self.digests.get((provider_id, dataset_key, natural_key))

    def record_source_revision(self, **kwargs) -> None:
        self.revisions.append(kwargs)

    def write_bars(self, result, *, run_id):
        self.bars_written += len(result.bars)
        return len(result.bars)

    def write_coverage(self, coverage, *, run_id):
        self.coverage_written += len(coverage)
        return len(coverage)

    def remember(self, provider_id, dataset_key, natural_key, digest, key) -> None:
        self.digests[(provider_id, dataset_key, natural_key)] = (digest, key)


# ------------------------------------------------------------------ Parquet
#: Matches the FakeFetcher exactly: same provider, same dataset, same reading of
#: the terms. Anything less than an exact match is refused, which is the point.
_ALLOWED = PersistenceDecision(
    provider_id=PROVIDER,
    dataset_key=DATASET,
    permission=Permission.ALLOWED,
    policy_version="ecb-2026-09-16",
)


def test_a_day_round_trips_through_parquet_without_losing_precision(tmp_path):
    """Prices are exact quantities; a float round trip would move the boundary."""

    store = ParquetSeriesStore(LocalObjectStore(tmp_path))
    day = date(2026, 9, 16)
    bars = [_bar(day, "13010", "2999.995"), _bar(day, "13020", "3000.005")]

    written = store.write_day(
        bars, provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET, trade_date=day
    )
    read_back = store.read_day(
        provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET, trade_date=day
    )

    assert written.rows == 2
    assert [b.close for b in read_back] == [Decimal("2999.99500000"), Decimal("3000.00500000")]
    assert [b.native_symbol for b in read_back] == ["13010", "13020"]
    assert read_back[0].close_basis is PriceBasis.RAW


def test_the_partition_path_says_what_the_file_is_about(tmp_path):
    prefix = partition_prefix(PROVIDER, MARKET, DATASET, date(2026, 9, 16))
    assert prefix.endswith("provider=testfeed/market=JP/dataset=ECB_EXR_DAILY/year=2026/month=09/date=2026-09-16")


def test_writing_the_same_day_twice_is_one_object_and_a_correction_is_two(tmp_path):
    store = ParquetSeriesStore(LocalObjectStore(tmp_path))
    day = date(2026, 9, 16)
    bars = [_bar(day, "13010", "1000")]

    first = store.write_day(bars, provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET, trade_date=day)
    again = store.write_day(bars, provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET, trade_date=day)
    assert first.key == again.key
    assert again.created is False

    corrected = store.write_day(
        [_bar(day, "13010", "1010")], provider_id=PROVIDER, market_code=MARKET,
        dataset_key=DATASET, trade_date=day,
    )
    assert corrected.key != first.key, "a correction lands beside the original, never over it"
    assert len(store.list_day(provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET, trade_date=day)) == 2


def test_a_partition_refuses_bars_from_another_day(tmp_path):
    store = ParquetSeriesStore(LocalObjectStore(tmp_path))
    with pytest.raises(ValueError, match="a day partition holds one day"):
        store.write_day(
            [_bar(date(2026, 9, 15), "13010", "1000")],
            provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET, trade_date=date(2026, 9, 16),
        )


def test_an_empty_partition_is_refused_because_an_empty_day_is_a_finding(tmp_path):
    store = ParquetSeriesStore(LocalObjectStore(tmp_path))
    with pytest.raises(ValueError, match="empty day is a finding"):
        store.write_day([], provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET, trade_date=date(2026, 9, 16))


def test_a_range_read_returns_only_the_days_asked_for(tmp_path):
    store = ParquetSeriesStore(LocalObjectStore(tmp_path))
    for day in (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)):
        store.write_day([_bar(day, "13010", "1000")], provider_id=PROVIDER, market_code=MARKET,
                        dataset_key=DATASET, trade_date=day)

    bars = store.read_range(
        provider_id=PROVIDER, market_code=MARKET, dataset_key=DATASET,
        start=date(2026, 9, 15), end=date(2026, 9, 16),
    )
    assert sorted({b.trade_date for b in bars}) == [date(2026, 9, 15), date(2026, 9, 16)]


# ---------------------------------------------------------------- ingestion
def test_a_day_is_stored_raw_manifested_and_written_as_parquet(tmp_path):
    store = LocalObjectStore(tmp_path)
    writer = FakeWriter()
    job = MarketIngestJob(
        FakeFetcher(), store=store, parquet=ParquetSeriesStore(store), writer=writer, run_id=None, persistence=_ALLOWED
    )

    outcome = job.ingest_day(date(2026, 9, 16))

    assert outcome.ok is True
    assert outcome.rows == 2
    assert outcome.raw_object_key.startswith("raw/testfeed/")
    assert outcome.partition is not None
    assert writer.bars_written == 2
    assert writer.coverage_written == 2
    assert len(writer.raw_objects) == 1
    assert writer.raw_objects[0].data_from == date(2026, 9, 16)


def test_a_transient_failure_is_retried_and_a_permanent_one_is_not():
    flaky = FakeFetcher(fail_times=2)
    job = MarketIngestJob(flaky, store=LocalObjectStore("/tmp/ignored"), sleep=lambda _s: None, persistence=_ALLOWED)
    # the store is never touched on the failing attempts
    outcome = job.ingest_day(date(2026, 9, 16))
    assert outcome.ok is True
    assert outcome.attempts == 3

    permanent = FakeFetcher(fail_times=5, error=ValueError("404 not found"))
    job = MarketIngestJob(permanent, store=LocalObjectStore("/tmp/ignored"), sleep=lambda _s: None, persistence=_ALLOWED)
    outcome = job.ingest_day(date(2026, 9, 16))
    assert outcome.ok is False
    assert outcome.error_class == PERMANENT_ERROR
    assert outcome.attempts == 1, "a 404 will not become a 200 by asking again"


def test_retries_give_up_and_say_so(tmp_path):
    job = MarketIngestJob(
        FakeFetcher(fail_times=99),
        store=LocalObjectStore(tmp_path),
        max_attempts=2,
        sleep=lambda _s: None,
        persistence=_ALLOWED,
    )
    outcome = job.ingest_day(date(2026, 9, 16))
    assert outcome.ok is False
    assert outcome.error_class == TRANSIENT_ERROR
    assert outcome.attempts == 2


def test_an_empty_payload_is_a_failure_not_a_quiet_success(tmp_path):
    """A holiday and an outage both produce no rows. Only one of them is fine."""

    job = MarketIngestJob(FakeFetcher(empty=True), store=LocalObjectStore(tmp_path), persistence=_ALLOWED)
    outcome = job.ingest_day(date(2026, 9, 16))

    assert outcome.ok is False
    assert outcome.error_class == EMPTY_RESULT
    assert outcome.raw_object_key is not None, "the raw payload is kept even so"


def test_a_changed_payload_for_the_same_day_is_recorded_as_a_revision(tmp_path):
    store = LocalObjectStore(tmp_path)
    writer = FakeWriter()
    fetcher = FakeFetcher()
    job = MarketIngestJob(fetcher, store=store, writer=writer, persistence=_ALLOWED)

    first = job.ingest_day(date(2026, 9, 16))
    writer.remember(PROVIDER, DATASET, "JP/2026-09-16", first.new_sha256, first.raw_object_key)
    assert first.revision_detected is False

    # the provider corrects the day, silently
    fetcher.body_suffix = b" (corrected)"
    second = job.ingest_day(date(2026, 9, 16))

    assert second.revision_detected is True
    assert second.previous_sha256 == first.new_sha256
    assert len(writer.revisions) == 1
    assert writer.revisions[0]["previous_sha256"] != writer.revisions[0]["new_sha256"]


def test_an_unchanged_refetch_is_not_a_revision(tmp_path):
    store = LocalObjectStore(tmp_path)
    writer = FakeWriter()
    job = MarketIngestJob(FakeFetcher(), store=store, writer=writer, persistence=_ALLOWED)

    first = job.ingest_day(date(2026, 9, 16))
    writer.remember(PROVIDER, DATASET, "JP/2026-09-16", first.new_sha256, first.raw_object_key)
    second = job.ingest_day(date(2026, 9, 16))

    assert second.revision_detected is False
    assert writer.revisions == []


def test_a_backfill_keeps_going_past_one_bad_day(tmp_path):
    class SometimesEmpty(FakeFetcher):
        def fetch_day(self, trade_date):
            self._empty = trade_date == date(2026, 9, 15)
            return super().fetch_day(trade_date)

    store = LocalObjectStore(tmp_path)
    job = MarketIngestJob(SometimesEmpty(), store=store, parquet=ParquetSeriesStore(store), persistence=_ALLOWED)
    report = job.backfill([date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)])

    assert len(report.succeeded) == 2
    assert len(report.failed) == 1
    assert report.rows == 4
    assert report.as_dict()["days_failed"] == 1


def test_a_backfill_can_stop_when_every_remaining_day_will_fail_too(tmp_path):
    job = MarketIngestJob(
        FakeFetcher(fail_times=99, error=ValueError("403 forbidden")),
        store=LocalObjectStore(tmp_path), sleep=lambda _s: None, persistence=_ALLOWED
    )
    report = job.backfill(trading_days(date(2026, 1, 1), date(2026, 3, 31)), stop_after_failures=3)

    assert len(report.days) == 3, "a dead credential should not walk a whole quarter"


def test_the_incremental_run_refetches_a_window_without_repeating_the_latest_day(tmp_path):
    store = LocalObjectStore(tmp_path)
    fetcher = FakeFetcher()
    job = MarketIngestJob(fetcher, store=store, parquet=ParquetSeriesStore(store), persistence=_ALLOWED)

    report = job.incremental(
        latest=date(2026, 9, 16),
        revision_window=[date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)],
    )

    assert fetcher.calls == [date(2026, 9, 16), date(2026, 9, 14), date(2026, 9, 15)]
    assert len(report.succeeded) == 3


def test_coverage_is_computed_from_what_arrived():
    result = NormalisationResult(
        bars=[_bar(date(2026, 9, 15), "13010", "1000"), _bar(date(2026, 9, 16), "13010", "1010")]
    )
    coverage = compute_coverage(result, observed_at=T0)

    assert len(coverage) == 1
    assert coverage[0].native_symbol == "13010"
    assert coverage[0].first_trade_date == date(2026, 9, 15)
    assert coverage[0].last_trade_date == date(2026, 9, 16)
    assert coverage[0].bar_count == 2


def test_the_idempotency_key_is_the_logical_invocation():
    base = dict(
        job_name="jp_eod_fetch", run_mode="PRODUCTION", market_code="JP", provider_id="testfeed",
        dataset_key=DATASET, trade_date=date(2026, 9, 16), config_hash="cfg",
    )
    assert idempotency_key(**base) == idempotency_key(**base), "a retry is the same invocation"
    assert idempotency_key(**base) != idempotency_key(**{**base, "trade_date": date(2026, 9, 15)})
    assert idempotency_key(**base) != idempotency_key(**{**base, "provider_id": "other"})
    assert idempotency_key(**base) != idempotency_key(**{**base, "config_hash": "changed"})


# --------------------------------------------------------------- scheduling
def test_the_scheduler_emits_the_jobs_that_are_due():
    scheduler = CalendarScheduler(env={})
    # a Wednesday, late enough for everything
    requests = scheduler.tick(datetime(2026, 9, 16, 23, 59, tzinfo=UTC))

    names = {r.job_name for r in requests}
    assert "fx_fetch" in names
    assert "price_eligibility" in names
    assert "stage1_screening" in names


def test_nothing_is_due_before_its_time():
    scheduler = CalendarScheduler(env={})
    early = scheduler.tick(datetime(2026, 9, 16, 0, 1, tzinfo=UTC))
    assert all(r.job_name != "fx_fetch" for r in early)


def test_the_scheduler_is_quiet_at_the_weekend():
    scheduler = CalendarScheduler(env={})
    saturday = scheduler.tick(datetime(2026, 9, 19, 23, 59, tzinfo=UTC))
    assert saturday == []


def test_a_job_whose_credential_is_absent_is_skipped_with_the_variable_named():
    """The zero-cost core must not be blocked by an optional paid provider."""

    definitions = {
        "paid_thing": JobDefinition(
            "paid_thing", "needs a subscription", markets=("JP",),
            required_env=("SURGE_SOME_PAID_TOKEN",), due_at_utc_minutes=0,
        )
    }
    scheduler = CalendarScheduler(definitions=definitions, env={})
    requests = scheduler.tick(datetime(2026, 9, 16, 12, 0, tzinfo=UTC))

    assert requests == []
    assert scheduler.skipped == [("paid_thing", "missing credentials: SURGE_SOME_PAID_TOKEN")]

    with_credential = CalendarScheduler(definitions=definitions, env={"SURGE_SOME_PAID_TOKEN": "x"})
    assert len(with_credential.tick(datetime(2026, 9, 16, 12, 0, tzinfo=UTC))) == 1


def test_two_ticks_of_the_same_day_produce_the_same_idempotency_key():
    scheduler = CalendarScheduler(env={}, id_factory=lambda: "fixed")
    first = scheduler.tick(datetime(2026, 9, 16, 23, 59, tzinfo=UTC))
    second = scheduler.tick(datetime(2026, 9, 16, 23, 59, 30, tzinfo=UTC))

    assert [r.idempotency_key for r in first] == [r.idempotency_key for r in second]


def test_the_idempotency_key_changes_when_the_configuration_does():
    a = build_idempotency_key(
        job_name="j", run_mode=RunMode.PRODUCTION, market="JP", as_of_date=date(2026, 9, 16),
        config_fingerprint=config_hash({"threshold": 3000}), job_version="v1",
    )
    b = build_idempotency_key(
        job_name="j", run_mode=RunMode.PRODUCTION, market="JP", as_of_date=date(2026, 9, 16),
        config_fingerprint=config_hash({"threshold": 2000}), job_version="v1",
    )
    assert a != b


def test_config_hash_ignores_key_order():
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


def test_the_local_runner_refuses_to_run_the_same_invocation_twice():
    calls: list[str] = []
    runner = LocalJobRunner({"fx_fetch": lambda request: calls.append(request.job_name) or {}})
    scheduler = CalendarScheduler(env={})
    request = next(r for r in scheduler.tick(datetime(2026, 9, 16, 23, 59, tzinfo=UTC)) if r.job_name == "fx_fetch")

    assert runner.submit(request).status is JobStatus.SUCCEEDED
    assert runner.submit(request).status is JobStatus.SKIPPED
    assert calls == ["fx_fetch"]


def test_an_unhandled_job_is_skipped_rather_than_crashing_the_tick():
    runner = LocalJobRunner({})
    scheduler = CalendarScheduler(env={})
    request = scheduler.tick(datetime(2026, 9, 16, 23, 59, tzinfo=UTC))[0]
    assert runner.submit(request).status is JobStatus.SKIPPED


def test_a_handler_that_raises_becomes_a_failed_result_not_an_exception():
    def explode(_request):
        raise RuntimeError("the provider fell over")

    runner = LocalJobRunner({"fx_fetch": explode})
    scheduler = CalendarScheduler(env={})
    request = next(r for r in scheduler.tick(datetime(2026, 9, 16, 23, 59, tzinfo=UTC)) if r.job_name == "fx_fetch")
    result = runner.submit(request)

    assert result.status is JobStatus.FAILED
    assert "fell over" in result.detail


def test_trading_days_are_weekdays_minus_the_holidays_the_caller_knows():
    days = trading_days(date(2026, 9, 14), date(2026, 9, 20))
    assert days == [date(2026, 9, d) for d in (14, 15, 16, 17, 18)]

    with_holiday = trading_days(date(2026, 9, 14), date(2026, 9, 20), holidays=[date(2026, 9, 16)])
    assert date(2026, 9, 16) not in with_holiday


def test_next_due_skips_to_the_next_scheduled_weekday():
    friday_evening = datetime(2026, 9, 18, 23, 59, tzinfo=UTC)
    due = next_due(JOB_DEFINITIONS["fx_fetch"], friday_evening)
    assert due.weekday() == 0, "the next one is Monday, not Saturday"


def test_every_defined_job_names_the_markets_it_covers():
    for definition in JOB_DEFINITIONS.values():
        assert definition.markets
        assert definition.description
        assert 0 <= definition.due_at_utc_minutes < 24 * 60


# --------------------------------------------- persistence is not optional


def test_a_job_cannot_be_built_without_saying_whether_the_data_may_be_kept():
    """No default, because a default would answer the question - permissively,
    silently, for every provider added afterwards."""

    with pytest.raises(TypeError, match="persistence"):
        MarketIngestJob(FakeFetcher(), store=LocalObjectStore("/tmp/ignored"))


@pytest.mark.parametrize(
    "permission",
    [Permission.NOT_SPECIFIED, Permission.UNKNOWN, Permission.PROHIBITED],
)
def test_nothing_is_written_when_the_terms_do_not_permit_keeping_it(tmp_path, permission):
    """NOT_SPECIFIED blocks exactly as PROHIBITED does. Terms that do not mention
    private storage have not agreed to it, and the ingest job is where that has
    to hold - by the time a caller is choosing to write, it is too late."""

    store = LocalObjectStore(tmp_path)
    job = MarketIngestJob(
        FakeFetcher(),
        store=store,
        parquet=ParquetSeriesStore(store),
        persistence=PersistenceDecision(
            provider_id=PROVIDER,
            dataset_key=DATASET,
            permission=permission,
            policy_version="ecb-2026-09-16",
            terms_url="https://example.invalid/terms",
        ),
    )

    with pytest.raises(LicenseViolation, match="private persistence"):
        job.ingest_day(date(2026, 9, 16))

    assert list(tmp_path.rglob("*")) == []


def test_an_unknown_decision_says_what_is_missing():
    decision = PersistenceDecision.unknown("some_provider", "SOME_DATASET")

    assert not decision.may_persist
    with pytest.raises(LicenseViolation, match="no licence policy was supplied"):
        decision.assert_may_persist(target="the object store")


# -------------------------------------- a permission is for one thing only


def _decision(**overrides) -> PersistenceDecision:
    base = {
        "provider_id": PROVIDER,
        "dataset_key": DATASET,
        "permission": Permission.ALLOWED,
        "policy_version": "ecb-2026-09-16",
    }
    base.update(overrides)
    return PersistenceDecision(**base)


def test_a_genuine_permission_for_another_provider_is_refused(tmp_path):
    """The ECB decision is real and correct - and it is not about Alpaca. Without
    this, a true permission would appear in the audit trail for a dataset it was
    never written about."""

    with pytest.raises(LicenseViolation, match="about provider"):
        MarketIngestJob(
            FakeFetcher(),
            store=LocalObjectStore(tmp_path),
            persistence=_decision(provider_id="alpaca_historical_sip"),
        )


def test_the_right_provider_with_the_wrong_dataset_is_refused(tmp_path):
    with pytest.raises(LicenseViolation, match="about dataset"):
        MarketIngestJob(
            FakeFetcher(),
            store=LocalObjectStore(tmp_path),
            persistence=_decision(dataset_key="SOME_OTHER_DATASET"),
        )


def test_a_stale_policy_version_is_refused_at_write_time(tmp_path):
    """A decision made under one reading of the terms does not authorise a write
    governed by another. The provider and dataset match, so this can only be
    caught where the fetch names the policy it was governed by."""

    store = LocalObjectStore(tmp_path)
    job = MarketIngestJob(
        FakeFetcher(),
        store=store,
        parquet=ParquetSeriesStore(store),
        persistence=_decision(policy_version="ecb-2025-01-01"),
    )

    with pytest.raises(LicenseViolation, match="under policy"):
        job.ingest_day(date(2026, 9, 16))

    assert list(tmp_path.rglob("*")) == []


def test_a_decision_with_no_policy_version_cannot_authorise_a_durable_write(tmp_path):
    """Absent is a mismatch, not a wildcard."""

    store = LocalObjectStore(tmp_path)
    job = MarketIngestJob(
        FakeFetcher(),
        store=store,
        persistence=_decision(policy_version=None),
    )

    with pytest.raises(LicenseViolation, match="names no policy_version"):
        job.ingest_day(date(2026, 9, 16))


def test_an_exactly_matching_allowed_policy_writes(tmp_path):
    store = LocalObjectStore(tmp_path)
    job = MarketIngestJob(FakeFetcher(), store=store, persistence=_ALLOWED)

    outcome = job.ingest_day(date(2026, 9, 16))

    assert outcome.ok
    assert outcome.rows == 2


def test_the_standard_way_in_is_built_from_the_stored_policy():
    """Building a decision by hand is what makes a mismatch possible; the
    constructor that cannot mismatch is the one to reach for."""

    from surge.licensing import LicenseMode, LicensePolicy, Obligation

    policy = LicensePolicy(
        provider_id=PROVIDER,
        dataset_key=DATASET,
        policy_version="ecb-2026-09-16",
        license_mode=LicenseMode.PUBLIC_DOMAIN,
        public_display_allowed=Permission.ALLOWED,
        third_party_access_allowed=Permission.ALLOWED,
        commercial_use_allowed=Permission.ALLOWED,
        academic_use_allowed=Permission.ALLOWED,
        raw_redistribution_allowed=Permission.ALLOWED,
        derived_output_sharing_allowed=Permission.ALLOWED,
        delete_on_cancel=Obligation.NOT_REQUIRED,
        delete_on_downgrade=Obligation.NOT_REQUIRED,
        attribution_required=Obligation.REQUIRED,
        modification_disclosure_required=Obligation.NOT_REQUIRED,
        entitlement_plan="public",
        terms_url="https://example.invalid/terms",
        private_persistence_allowed=Permission.ALLOWED,
    )

    decision = PersistenceDecision.from_license_policy(policy)

    assert decision.may_persist
    decision.assert_governs_fetch(
        provider_id=PROVIDER, dataset_key=DATASET, policy_version="ecb-2026-09-16"
    )
