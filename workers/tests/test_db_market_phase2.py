"""Phase 2 / Phase 3 database behaviour: cost, roles, eligibility, Stage 1.

The most valuable tests here are the parity ones. The filter threshold and the
route thresholds exist in two places - a config table the runs read, and a Python
default the jobs fall back to - and a silent divergence between them would mean
a backtest and a live run disagreeing for no visible reason.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from surge.features.engine import compute_features  # noqa: E402
from surge.licensing import AvailabilityBasis  # noqa: E402
from surge.market.db import PostgresMarketWriter  # noqa: E402
from surge.market.eligibility import DEFAULT_RULE, EligibilityDecision, evaluate  # noqa: E402
from surge.market.models import (  # noqa: E402
    CanonicalAction,
    CanonicalBar,
    CanonicalCoverage,
    CorporateActionType,
    NormalisationResult,
    PriceBasis,
)
from surge.market.series import build_comparable_series  # noqa: E402
from surge.routes.engine import ROUTE_DEFINITIONS, evaluate_routes  # noqa: E402
from test_db_master_semantics import T1, _new_run  # noqa: E402

DSN = os.environ.get("SURGE_TEST_DATABASE_URL")
WORKER_DSN = os.environ.get("SURGE_TEST_WORKER_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SURGE_TEST_DATABASE_URL is not set"),
]

READ_ONLY = psycopg2.errors.ReadOnlySqlTransaction
DENIED = psycopg2.errors.InsufficientPrivilege
NOW = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _raw_object(cur, run_id, *, provider="ecb", dataset="ECB_EXR_DAILY", policy="ecb-2026-09-16",
                plan="public", suffix: str | None = None) -> str:
    digest = (suffix or uuid.uuid4().hex).ljust(64, "0")[:64]
    key = f"raw/{provider}/{dataset}/{digest}.json"
    cur.execute(
        """
        insert into market.raw_objects (
          object_key, store_id, provider_id, dataset_key, license_policy_version,
          entitlement_plan, required_min_plan, sha256, bytes, content_type,
          request_url, observed_at, available_at, availability_basis, run_id
        ) values (%s, 'test', %s, %s, %s, %s, %s, %s, 10, 'application/json',
                  'https://example.invalid/f', %s, %s, 'OBSERVED_NOW', %s)
        """,
        (key, provider, dataset, policy, plan, plan, digest, T1, T1, str(run_id)),
    )
    return key


def _bar(day: date, symbol: str, close: str, *, key: str, market: str = "JP") -> CanonicalBar:
    price = Decimal(close)
    return CanonicalBar(
        provider_id="ecb", dataset_key="ECB_EXR_DAILY", market_code=market, native_symbol=symbol,
        trade_date=day, currency="JPY" if market == "JP" else "USD",
        open=price, high=price, low=price, close=price,
        volume=Decimal(1000), turnover=Decimal(100000),
        open_basis=PriceBasis.RAW, high_basis=PriceBasis.RAW, low_basis=PriceBasis.RAW,
        close_basis=PriceBasis.RAW, volume_basis=PriceBasis.RAW,
        observed_at=NOW, available_at=NOW, availability_basis=AvailabilityBasis.OBSERVED_NOW,
        raw_object_key=key, provider_security_id="PSID-1", identity_version="identity-1.1a",
        source_data_version="sdv-1",
    )


def _publish(cur, run_id) -> None:
    cur.execute(
        "insert into pipeline.run_publications (run_id, validation_version, validation_summary) "
        "values (%s, 'test-publication', '{}'::jsonb)",
        (str(run_id),),
    )


# --------------------------------------------------------------- cost model
def test_the_core_costs_nothing_to_keep_running(conn):
    with conn.cursor() as cur:
        cur.execute("select monthly_cost_jpy, enabled_paid_providers from market.recurring_cost")
        monthly, paid = cur.fetchone()

    assert monthly == 0, "an enabled paid provider would show up here as a number"
    assert paid == 0
    conn.rollback()


def test_the_paid_adapters_are_kept_but_not_enabled(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select provider_id, cost_class from market.providers where cost_class = 'OPTIONAL_PAID' "
            "order by provider_id"
        )
        assert [row[0] for row in cur.fetchall()] == ["eodhd", "jquants"]

        cur.execute(
            "select count(*) from market.provider_role_bindings "
            "where provider_id in ('eodhd', 'jquants') and enabled"
        )
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_the_roles_no_free_provider_fills_are_named_rather_than_silent(conn):
    with conn.cursor() as cur:
        cur.execute("select role from market.unfilled_roles order by 1")
        unfilled = {row[0] for row in cur.fetchall()}

    # The zero-cost search is still open for these, and the schema says so.
    assert "EOD_CURRENT_JP" in unfilled
    assert "EOD_CURRENT_US" in unfilled
    # while the roles the free sources already fill are not listed
    assert "FX_USDJPY" not in unfilled
    assert "SECURITY_MASTER_US" not in unfilled
    conn.rollback()


def test_a_role_resolves_to_its_provider_and_an_unfilled_one_to_nothing(conn):
    with conn.cursor() as cur:
        cur.execute("select market.provider_for_role('FX_USDJPY')")
        assert cur.fetchone()[0] == "ecb"

        cur.execute("select market.provider_for_role('EOD_CURRENT_JP')")
        assert cur.fetchone()[0] is None
    conn.rollback()


# ------------------------------------------------------------ config parity
def test_the_stored_filter_rule_matches_the_one_the_jobs_default_to(conn):
    """Two sources of truth that disagree would make a backtest unreproducible."""

    with conn.cursor() as cur:
        rule = PostgresMarketWriter(conn).load_filter_rule(DEFAULT_RULE.rule_version)

    assert rule.threshold_jpy == DEFAULT_RULE.threshold_jpy
    assert rule.max_price_age_days == DEFAULT_RULE.max_price_age_days
    assert rule.max_fx_age_seconds == DEFAULT_RULE.max_fx_age_seconds
    conn.rollback()


def test_the_stored_route_thresholds_match_the_engine(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select route_code, thresholds from screening.route_definitions "
            "where route_version = 'route-1.0.0' order by route_code"
        )
        stored = {code: thresholds for code, thresholds in cur.fetchall()}

    assert set(stored) == set(ROUTE_DEFINITIONS)
    for code, definition in ROUTE_DEFINITIONS.items():
        db_thresholds = dict(stored[code])
        min_bars = db_thresholds.pop("min_bars", None)
        assert min_bars == definition.min_bars, f"route {code} disagrees about its warmup"
        assert db_thresholds == definition.thresholds, f"route {code} thresholds diverged"
    conn.rollback()


# ------------------------------------------------------------- market writes
def test_bars_actions_and_coverage_all_land(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _raw_object(cur, run, suffix="a" * 8)

    writer = PostgresMarketWriter(conn)
    day = date(2026, 9, 16)
    result = NormalisationResult(
        bars=[_bar(day, "13010", "1000", key=key)],
        actions=[
            CanonicalAction(
                provider_id="ecb", dataset_key="ECB_EXR_DAILY", market_code="JP",
                native_symbol="13010", action_type=CorporateActionType.SPLIT, ex_date=day,
                split_to=Decimal(2), split_from=Decimal(1), observed_at=NOW, available_at=NOW,
                raw_object_key=key, provider_native_type="split",
            )
        ],
    )
    writer.write_bars(result, run_id=str(run))
    writer.write_coverage(
        [
            CanonicalCoverage(
                provider_id="ecb", dataset_key="ECB_EXR_DAILY", market_code="JP",
                native_symbol="13010", first_trade_date=day, last_trade_date=day, bar_count=1,
                observed_at=NOW, available_at=NOW, raw_object_key=key,
            )
        ],
        run_id=str(run),
    )

    with conn.cursor() as cur:
        cur.execute(
            "select close, close_basis, volume_basis, provider_security_id, identity_version "
            "from market.daily_bars where native_symbol = '13010' and trade_date = %s",
            (day,),
        )
        close, close_basis, volume_basis, psid, identity = cur.fetchone()
        assert close == Decimal("1000.000000")
        assert close_basis == "RAW"
        assert volume_basis == "RAW"
        assert psid == "PSID-1", "the provider's own id is a second recovery key"
        assert identity == "identity-1.1a"

        cur.execute("select action_type, split_to from market.corporate_actions where native_symbol = '13010'")
        assert cur.fetchone() == ("SPLIT", Decimal("2.0000000000"))

        cur.execute("select bar_count from market.security_coverage where native_symbol = '13010'")
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_a_changed_bar_still_records_a_revision_through_the_writer(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        key = _raw_object(cur, run, suffix="b" * 8)

    writer = PostgresMarketWriter(conn)
    day = date(2026, 9, 16)
    writer.write_bars(NormalisationResult(bars=[_bar(day, "99990", "1000", key=key)]), run_id=str(run))
    writer.write_bars(NormalisationResult(bars=[_bar(day, "99990", "1010", key=key)]), run_id=str(run))

    with conn.cursor() as cur:
        cur.execute(
            "select previous_values ->> 'close', new_values ->> 'close' "
            "from market.daily_bar_revisions where native_symbol = '99990'"
        )
        previous, new = cur.fetchone()
        assert float(previous) == pytest.approx(1000)
        assert float(new) == pytest.approx(1010)
    conn.rollback()


# --------------------------------------------------------------- eligibility
def _write_eligibility(cur, conn, *, market="JP", publish=True):
    run = _new_run(cur, market, T1)
    cur.execute("update pipeline.runs set job_name = 'price_eligibility', as_of_date = %s where run_id = %s",
                (date(2026, 9, 16), str(run)))
    key = _raw_object(cur, run, suffix=uuid.uuid4().hex[:8])

    day = date(2026, 9, 16)
    results = [
        ("13010", evaluate(market_code=market, as_of_date=day, knowledge_cutoff=NOW,
                           bar=_bar(day, "13010", "1000", key=key, market=market))),
        ("13020", evaluate(market_code=market, as_of_date=day, knowledge_cutoff=NOW,
                           bar=_bar(day, "13020", "5000", key=key, market=market))),
        ("13030", evaluate(market_code=market, as_of_date=day, knowledge_cutoff=NOW, bar=None)),
    ]
    PostgresMarketWriter(conn).write_eligibility(
        results, run_id=str(run), market_code=market, provider_id="ecb",
        as_of_date=day, knowledge_cutoff=NOW,
        universe_decisions={"13010": "INCLUDED", "13020": "INCLUDED", "13030": "UNRESOLVED"},
    )
    if publish:
        _publish(cur, run)
    return run, day


def test_the_three_outcomes_are_stored_distinctly(conn):
    with conn.cursor() as cur:
        run, day = _write_eligibility(cur, conn)

        cur.execute(
            "select decision, count(*) from market.price_eligibility where run_id = %s group by 1 order by 1",
            (str(run),),
        )
        assert dict(cur.fetchall()) == {
            "PRICE_ABOVE_3000": 1, "PRICE_ELIGIBLE": 1, "PRICE_MISSING": 1
        }

        cur.execute("select * from market.eligibility_coverage where run_id = %s", (str(run),))
        row = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
        assert row["evaluated"] == 3
        assert row["eligible"] == 1
        assert row["price_missing"] == 1
        assert row["unresolved_identity"] == 1
    conn.rollback()


def test_an_unresolved_security_is_priced_but_not_in_the_prediction_universe(conn):
    with conn.cursor() as cur:
        run, _ = _write_eligibility(cur, conn)

        cur.execute(
            "select count(*) from market.price_eligibility where run_id = %s and universe_decision = 'UNRESOLVED'",
            (str(run),),
        )
        assert cur.fetchone()[0] == 1, "UNRESOLVED securities are still evaluated and stored"

        cur.execute(
            "select count(*) from market.prediction_universe where run_id = %s and universe_decision <> 'INCLUDED'",
            (str(run),),
        )
        assert cur.fetchone()[0] == 0, "but they are not part of the prediction universe"
    conn.rollback()


def test_eligibility_is_read_through_the_publication_not_by_newest_run(conn):
    with conn.cursor() as cur:
        run, day = _write_eligibility(cur, conn)

        cur.execute("select count(*) from market.price_eligibility_as_of('JP', %s, now())", (day,))
        assert cur.fetchone()[0] == 3

        # a knowledge cutoff before the publication sees nothing
        cur.execute(
            "select count(*) from market.price_eligibility_as_of('JP', %s, %s)",
            (day, NOW - timedelta(days=365)),
        )
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_an_unpublished_eligibility_run_is_not_authoritative(conn):
    with conn.cursor() as cur:
        run, day = _write_eligibility(cur, conn, publish=False)
        cur.execute("select count(*) from market.price_eligibility_as_of('JP', %s, now())", (day,))
        assert cur.fetchone()[0] == 0, "a finished run is not the answer until it is published"
    conn.rollback()


def test_a_published_eligibility_run_is_frozen(conn):
    with conn.cursor() as cur:
        run, _ = _write_eligibility(cur, conn)

        with pytest.raises(READ_ONLY, match="immutable"):
            cur.execute(
                "update market.price_eligibility set decision = 'PRICE_ELIGIBLE' where run_id = %s",
                (str(run),),
            )
    conn.rollback()


def test_an_eligible_row_must_carry_the_numbers_that_produced_it(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        with pytest.raises(psycopg2.errors.CheckViolation, match="eligible_needs_price"):
            cur.execute(
                """
                insert into market.price_eligibility
                  (run_id, market_code, provider_id, native_symbol, as_of_date, decision,
                   rule_version, knowledge_cutoff)
                values (%s, 'JP', 'ecb', 'X', %s, 'PRICE_ELIGIBLE', 'price-filter-1.0.0', %s)
                """,
                (str(run), date(2026, 9, 16), NOW),
            )
    conn.rollback()


# ------------------------------------------------------------------ stage 1
def _series(symbol="13010", n=90):
    day = date(2026, 1, 5)
    bars = []
    for index in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price = Decimal(100 + index)
        bars.append(
            CanonicalBar(
                provider_id="ecb", dataset_key="ECB_EXR_DAILY", market_code="JP",
                native_symbol=symbol, trade_date=day, currency="JPY",
                open=price, high=price + 1, low=price - 1, close=price,
                volume=Decimal(1000 + index), turnover=Decimal(100000),
                open_basis=PriceBasis.RAW, high_basis=PriceBasis.RAW, low_basis=PriceBasis.RAW,
                close_basis=PriceBasis.RAW, volume_basis=PriceBasis.RAW,
                observed_at=NOW, available_at=NOW, availability_basis=AvailabilityBasis.OBSERVED_NOW,
            )
        )
        day += timedelta(days=1)
    return build_comparable_series(bars, [], as_of=bars[-1].trade_date)


def test_features_and_candidates_round_trip_through_the_database(conn):
    series = _series()
    features = compute_features(series)
    candidate = evaluate_routes(features, series=series)

    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        cur.execute("update pipeline.runs set job_name = 'stage1_screening' where run_id = %s", (str(run),))

    writer = PostgresMarketWriter(conn)
    assert writer.write_features([features], run_id=str(run), observed_at=NOW, available_at=NOW) == 1

    if candidate.is_candidate:
        assert writer.write_candidates(
            [candidate], run_id=str(run), observed_at=NOW, available_at=NOW,
            price_eligible={candidate.native_symbol: True},
            universe_decisions={candidate.native_symbol: "INCLUDED"},
        ) == 1

    with conn.cursor() as cur:
        cur.execute(
            "select bars_available, warmup_satisfied, series_basis, feature_version, rsi_14, sma_25 "
            "from screening.features_daily where run_id = %s",
            (str(run),),
        )
        bars_available, warmup, basis, version, rsi, sma25 = cur.fetchone()
        assert bars_available == 90
        assert warmup is True
        assert basis == "RAW_UNADJUSTED", "no split in range, so the raw series is already comparable"
        assert version == features.feature_version
        assert rsi is not None and sma25 is not None

        if candidate.is_candidate:
            cur.execute(
                "select discovery_routes, route_count, route_evidence, price_eligible "
                "from screening.route_candidates where run_id = %s",
                (str(run),),
            )
            routes, count, evidence, eligible = cur.fetchone()
            assert sorted(routes) == sorted(candidate.discovery_routes)
            assert count == len(routes)
            assert set(json.loads(json.dumps(evidence))) == set(candidate.discovery_routes)
            assert eligible is True
    conn.rollback()


def test_a_candidate_row_cannot_claim_routes_it_does_not_list(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        with pytest.raises(psycopg2.errors.CheckViolation, match="routes_ck"):
            cur.execute(
                """
                insert into screening.route_candidates (
                  run_id, market_code, provider_id, native_symbol, trade_date,
                  route_version, feature_version, discovery_routes, route_count,
                  observed_at, available_at, availability_basis
                ) values (%s, 'JP', 'ecb', 'X', %s, 'route-1.0.0', 'features-1.0.0',
                          array['A'], 3, %s, %s, 'OBSERVED_NOW')
                """,
                (str(run), date(2026, 9, 16), NOW, NOW),
            )
    conn.rollback()


def test_a_candidate_with_no_routes_is_not_a_candidate(conn):
    with conn.cursor() as cur:
        run = _new_run(cur, "JP", T1)
        with pytest.raises(psycopg2.errors.CheckViolation, match="nonempty_ck"):
            cur.execute(
                """
                insert into screening.route_candidates (
                  run_id, market_code, provider_id, native_symbol, trade_date,
                  route_version, feature_version, discovery_routes, route_count,
                  observed_at, available_at, availability_basis
                ) values (%s, 'JP', 'ecb', 'X', %s, 'route-1.0.0', 'features-1.0.0',
                          '{}'::text[], 0, %s, %s, 'OBSERVED_NOW')
                """,
                (str(run), date(2026, 9, 16), NOW, NOW),
            )
    conn.rollback()


# ------------------------------------------------------------------ privilege
def test_the_screening_schema_is_covered_by_the_public_execute_guard(conn):
    with conn.cursor() as cur:
        cur.execute("select 'screening' = any (pipeline.project_schemas())")
        assert cur.fetchone()[0] is True

        cur.execute(
            """
            select n.nspname || '.' || p.proname
            from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = any (pipeline.project_schemas())
              and has_function_privilege('public', p.oid, 'EXECUTE')
            """
        )
        assert cur.fetchall() == []
    conn.rollback()


@pytest.mark.skipif(not WORKER_DSN, reason="SURGE_TEST_WORKER_DSN is not set")
def test_the_worker_writes_market_data_but_still_cannot_delete_any_of_it():
    worker = psycopg2.connect(WORKER_DSN)
    worker.autocommit = False
    try:
        for statement in (
            "delete from market.daily_bars where native_symbol = 'nothing'",
            "delete from market.price_eligibility where native_symbol = 'nothing'",
            "delete from screening.features_daily where native_symbol = 'nothing'",
            "delete from screening.route_candidates where native_symbol = 'nothing'",
        ):
            with worker.cursor() as cur, pytest.raises((DENIED, READ_ONLY)):
                cur.execute(statement)
            worker.rollback()
    finally:
        worker.rollback()
        worker.close()
