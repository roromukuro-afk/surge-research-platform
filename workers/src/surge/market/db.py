"""The database side of market data: writes, and the reads a job needs.

Kept apart from the ingestion logic so the pipeline can be tested without a
database and the SQL can be tested without a provider. Everything here takes a
psycopg2 connection and leaves transaction control to the caller: a job commits
once, at the end, so a half-written day is never visible.

Nothing in this module decides anything. It writes what it is given and reads
what it is asked for; the judgements live in eligibility.py and the route engine.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from surge.features.engine import DailyFeatures
from surge.manifest import INSERT_RAW_OBJECT, RawObjectRecord, manifest_params
from surge.market.eligibility import EligibilityResult, FilterRule, FxObservation
from surge.market.models import (
    CanonicalAction,
    CanonicalAdjustedBar,
    CanonicalBar,
    CanonicalCoverage,
    NormalisationResult,
)
from surge.routes.engine import CandidateResult

INSERT_BAR = """
insert into market.daily_bars (
  provider_id, dataset_key, market_code, native_symbol, trade_date,
  exchange_code, currency, security_id,
  open, high, low, close, volume, turnover,
  open_basis, high_basis, low_basis, close_basis, volume_basis,
  observed_at, available_at, availability_basis, source_published_at,
  raw_object_key, run_id,
  provider_security_id, source_data_version, identity_version, source_timestamp
) values (
  %(provider_id)s, %(dataset_key)s, %(market_code)s, %(native_symbol)s, %(trade_date)s,
  %(exchange_code)s, %(currency)s, %(security_id)s,
  %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(turnover)s,
  %(open_basis)s, %(high_basis)s, %(low_basis)s, %(close_basis)s, %(volume_basis)s,
  %(observed_at)s, %(available_at)s, %(availability_basis)s, %(source_published_at)s,
  %(raw_object_key)s, %(run_id)s,
  %(provider_security_id)s, %(source_data_version)s, %(identity_version)s, %(source_timestamp)s
)
on conflict (provider_id, dataset_key, native_symbol, trade_date) do update set
  open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
  volume = excluded.volume, turnover = excluded.turnover,
  observed_at = excluded.observed_at, available_at = excluded.available_at,
  raw_object_key = excluded.raw_object_key, run_id = excluded.run_id,
  ingested_at = clock_timestamp()
"""

INSERT_ADJUSTED = """
insert into market.daily_bars_adjusted (
  provider_id, dataset_key, native_symbol, trade_date,
  adj_open, adj_high, adj_low, adj_close, adj_volume, adjusted_close,
  adjustment_basis, adjusted_close_basis, adjustment_factor, ex_event_code,
  recomputed_retroactively, observed_at, available_at, availability_basis,
  raw_object_key, run_id, provider_security_id, source_data_version
) values (
  %(provider_id)s, %(dataset_key)s, %(native_symbol)s, %(trade_date)s,
  %(adj_open)s, %(adj_high)s, %(adj_low)s, %(adj_close)s, %(adj_volume)s, %(adjusted_close)s,
  %(adjustment_basis)s, %(adjusted_close_basis)s, %(adjustment_factor)s, %(ex_event_code)s,
  %(recomputed_retroactively)s, %(observed_at)s, %(available_at)s, %(availability_basis)s,
  %(raw_object_key)s, %(run_id)s, %(provider_security_id)s, %(source_data_version)s
)
on conflict (provider_id, dataset_key, native_symbol, trade_date) do nothing
"""

INSERT_ACTION = """
insert into market.corporate_actions (
  provider_id, dataset_key, market_code, native_symbol, action_type, ex_date,
  record_date, payment_date, declaration_date,
  split_from, split_to, adjustment_factor, amount, unadjusted_amount, currency,
  provider_native_type, observed_at, available_at, availability_basis,
  raw_object_key, run_id, provider_security_id, source_data_version, from_symbol, to_symbol
) values (
  %(provider_id)s, %(dataset_key)s, %(market_code)s, %(native_symbol)s, %(action_type)s, %(ex_date)s,
  %(record_date)s, %(payment_date)s, %(declaration_date)s,
  %(split_from)s, %(split_to)s, %(adjustment_factor)s, %(amount)s, %(unadjusted_amount)s, %(currency)s,
  %(provider_native_type)s, %(observed_at)s, %(available_at)s, %(availability_basis)s,
  %(raw_object_key)s, %(run_id)s, %(provider_security_id)s, %(source_data_version)s,
  %(from_symbol)s, %(to_symbol)s
)
on conflict (provider_id, dataset_key, native_symbol, ex_date, action_type, provider_native_type)
  do nothing
"""

INSERT_COVERAGE = """
insert into market.security_coverage (
  provider_id, dataset_key, native_symbol, market_code,
  first_trade_date, last_trade_date, is_delisted, delisted_on,
  corporate_action_completeness, completeness_reason,
  observed_at, available_at, availability_basis, raw_object_key, run_id,
  provider_security_id, bar_count, first_seen_at, last_seen_at
) values (
  %(provider_id)s, %(dataset_key)s, %(native_symbol)s, %(market_code)s,
  %(first_trade_date)s, %(last_trade_date)s, %(is_delisted)s, %(delisted_on)s,
  %(corporate_action_completeness)s, %(completeness_reason)s,
  %(observed_at)s, %(available_at)s, %(availability_basis)s, %(raw_object_key)s, %(run_id)s,
  %(provider_security_id)s, %(bar_count)s, %(observed_at)s, %(observed_at)s
)
on conflict (provider_id, dataset_key, native_symbol) do update set
  first_trade_date = least(market.security_coverage.first_trade_date, excluded.first_trade_date),
  last_trade_date = greatest(market.security_coverage.last_trade_date, excluded.last_trade_date),
  bar_count = coalesce(market.security_coverage.bar_count, 0) + coalesce(excluded.bar_count, 0),
  last_seen_at = excluded.last_seen_at,
  run_id = excluded.run_id
"""

INSERT_SOURCE_REVISION = """
insert into market.source_revisions (
  provider_id, dataset_key, natural_key, previous_sha256, new_sha256,
  previous_object_key, new_object_key, detected_by_run_id, notes
) values (
  %(provider_id)s, %(dataset_key)s, %(natural_key)s, %(previous_sha256)s, %(new_sha256)s,
  %(previous_object_key)s, %(new_object_key)s, %(run_id)s, %(notes)s
)
"""

INSERT_ELIGIBILITY = """
insert into market.price_eligibility (
  run_id, market_code, provider_id, native_symbol, as_of_date,
  security_id, exchange_code, identity_version, universe_decision,
  decision, rule_version,
  price, price_currency, price_basis, price_trade_date,
  price_observed_at, price_available_at, price_age_days,
  fx_rate, fx_source_date, fx_observed_at, fx_available_at, fx_age_seconds,
  converted_jpy, universe_run_id, market_data_run_id, fx_run_id, knowledge_cutoff
) values (
  %(run_id)s, %(market_code)s, %(provider_id)s, %(native_symbol)s, %(as_of_date)s,
  %(security_id)s, %(exchange_code)s, %(identity_version)s, %(universe_decision)s,
  %(decision)s, %(rule_version)s,
  %(price)s, %(price_currency)s, %(price_basis)s, %(price_trade_date)s,
  %(price_observed_at)s, %(price_available_at)s, %(price_age_days)s,
  %(fx_rate)s, %(fx_source_date)s, %(fx_observed_at)s, %(fx_available_at)s, %(fx_age_seconds)s,
  %(converted_jpy)s, %(universe_run_id)s, %(market_data_run_id)s, %(fx_run_id)s, %(knowledge_cutoff)s
)
"""

INSERT_FX = """
insert into market.fx_rates (
  provider_id, dataset_key, source_date, eur_usd, eur_jpy, derived_usd_jpy,
  derivation_method, eur_usd_decimals, eur_jpy_decimals, rate_kind,
  source_published_at, observed_at, available_at, availability_basis,
  content_sha256, raw_object_key, run_id, notes
) values (
  %(provider_id)s, %(dataset_key)s, %(source_date)s, %(eur_usd)s, %(eur_jpy)s, %(derived_usd_jpy)s,
  %(derivation_method)s, %(eur_usd_decimals)s, %(eur_jpy_decimals)s, %(rate_kind)s,
  %(source_published_at)s, %(observed_at)s, %(available_at)s, %(availability_basis)s,
  %(content_sha256)s, %(raw_object_key)s, %(run_id)s, %(notes)s
)
on conflict (provider_id, dataset_key, source_date) do update set
  eur_usd = excluded.eur_usd,
  eur_jpy = excluded.eur_jpy,
  derived_usd_jpy = excluded.derived_usd_jpy,
  source_published_at = excluded.source_published_at,
  content_sha256 = excluded.content_sha256,
  observed_at = excluded.observed_at,
  run_id = excluded.run_id
"""


def _bar_params(bar: CanonicalBar, run_id: str | None) -> dict:
    return {
        "provider_id": bar.provider_id, "dataset_key": bar.dataset_key,
        "market_code": bar.market_code, "native_symbol": bar.native_symbol,
        "trade_date": bar.trade_date, "exchange_code": bar.exchange_code,
        "currency": bar.currency, "security_id": None,
        "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
        "volume": bar.volume, "turnover": bar.turnover,
        "open_basis": str(bar.open_basis), "high_basis": str(bar.high_basis),
        "low_basis": str(bar.low_basis), "close_basis": str(bar.close_basis),
        "volume_basis": str(bar.volume_basis),
        "observed_at": bar.observed_at, "available_at": bar.available_at,
        "availability_basis": str(bar.availability_basis),
        "source_published_at": None,
        "raw_object_key": bar.raw_object_key, "run_id": run_id,
        "provider_security_id": bar.provider_security_id,
        "source_data_version": bar.source_data_version,
        "identity_version": bar.identity_version,
        "source_timestamp": bar.source_timestamp,
    }


def _adjusted_params(row: CanonicalAdjustedBar, run_id: str | None) -> dict:
    return {
        "provider_id": row.provider_id, "dataset_key": row.dataset_key,
        "native_symbol": row.native_symbol, "trade_date": row.trade_date,
        "adj_open": row.adj_open, "adj_high": row.adj_high, "adj_low": row.adj_low,
        "adj_close": row.adj_close, "adj_volume": row.adj_volume,
        "adjusted_close": row.adjusted_close,
        "adjustment_basis": str(row.adjustment_basis),
        "adjusted_close_basis": None if row.adjusted_close_basis is None else str(row.adjusted_close_basis),
        "adjustment_factor": row.adjustment_factor, "ex_event_code": row.ex_event_code,
        "recomputed_retroactively": row.recomputed_retroactively,
        "observed_at": row.observed_at, "available_at": row.available_at,
        "availability_basis": str(row.availability_basis),
        "raw_object_key": row.raw_object_key, "run_id": run_id,
        "provider_security_id": row.provider_security_id,
        "source_data_version": row.source_data_version,
    }


def _action_params(action: CanonicalAction, run_id: str | None) -> dict:
    return {
        "provider_id": action.provider_id, "dataset_key": action.dataset_key,
        "market_code": action.market_code, "native_symbol": action.native_symbol,
        "action_type": str(action.action_type), "ex_date": action.ex_date,
        "record_date": action.record_date, "payment_date": action.payment_date,
        "declaration_date": action.declaration_date,
        "split_from": action.split_from, "split_to": action.split_to,
        "adjustment_factor": action.adjustment_factor, "amount": action.amount,
        "unadjusted_amount": action.unadjusted_amount, "currency": action.currency,
        "provider_native_type": action.provider_native_type,
        "observed_at": action.observed_at, "available_at": action.available_at,
        "availability_basis": str(action.availability_basis),
        "raw_object_key": action.raw_object_key, "run_id": run_id,
        "provider_security_id": action.provider_security_id,
        "source_data_version": action.source_data_version,
        "from_symbol": action.from_symbol, "to_symbol": action.to_symbol,
    }


def _coverage_params(coverage: CanonicalCoverage, run_id: str | None) -> dict:
    return {
        "provider_id": coverage.provider_id, "dataset_key": coverage.dataset_key,
        "native_symbol": coverage.native_symbol, "market_code": coverage.market_code,
        "first_trade_date": coverage.first_trade_date, "last_trade_date": coverage.last_trade_date,
        "is_delisted": coverage.is_delisted, "delisted_on": coverage.delisted_on,
        "corporate_action_completeness": coverage.corporate_action_completeness,
        "completeness_reason": coverage.completeness_reason,
        "observed_at": coverage.observed_at, "available_at": coverage.available_at,
        "availability_basis": str(coverage.availability_basis),
        "raw_object_key": coverage.raw_object_key, "run_id": run_id,
        "provider_security_id": coverage.provider_security_id,
        "bar_count": coverage.bar_count,
    }


class PostgresMarketWriter:
    """Implements the MarketWriter protocol against a psycopg2 connection."""

    def __init__(self, connection) -> None:
        self._connection = connection

    def record_raw_object(self, record: RawObjectRecord) -> None:
        with self._connection.cursor() as cur:
            cur.execute(INSERT_RAW_OBJECT, manifest_params(record))

    def last_digest(self, provider_id: str, dataset_key: str, natural_key: str) -> tuple[str, str] | None:
        """The digest of the last payload stored for this logical request.

        Matched on the manifest's own date range rather than on a separate
        bookkeeping table: the manifest already knows which object covered which
        day, and a second source of truth would be one more thing to drift.
        """

        day = natural_key.split("/")[-1]
        with self._connection.cursor() as cur:
            cur.execute(
                """
                select sha256, object_key
                from market.raw_objects
                where provider_id = %s and dataset_key = %s
                  and data_from = %s::date and data_to = %s::date
                order by created_at desc
                limit 1 offset 1
                """,
                (provider_id, dataset_key, day, day),
            )
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def write_bars(self, result: NormalisationResult, *, run_id: str | None) -> int:
        with self._connection.cursor() as cur:
            for bar in result.bars:
                cur.execute(INSERT_BAR, _bar_params(bar, run_id))
            for adjusted in result.adjusted:
                cur.execute(INSERT_ADJUSTED, _adjusted_params(adjusted, run_id))
            for action in result.actions:
                cur.execute(INSERT_ACTION, _action_params(action, run_id))
        return len(result.bars)

    def write_coverage(self, coverage: list[CanonicalCoverage], *, run_id: str | None) -> int:
        with self._connection.cursor() as cur:
            for row in coverage:
                cur.execute(INSERT_COVERAGE, _coverage_params(row, run_id))
        return len(coverage)

    def record_source_revision(self, **kwargs) -> None:
        with self._connection.cursor() as cur:
            cur.execute(INSERT_SOURCE_REVISION, kwargs)

    # --------------------------------------------------------------- reads
    def load_filter_rule(self, rule_version: str) -> FilterRule:
        with self._connection.cursor() as cur:
            cur.execute(
                "select rule_version, threshold_jpy, max_price_age_days, max_fx_age_seconds "
                "from market.price_filter_rules where rule_version = %s",
                (rule_version,),
            )
            row = cur.fetchone()
        if row is None:
            raise LookupError(f"no price filter rule named {rule_version!r}")
        return FilterRule(row[0], Decimal(row[1]), int(row[2]), int(row[3]))

    def latest_fx(self, knowledge_cutoff: datetime) -> FxObservation | None:
        with self._connection.cursor() as cur:
            cur.execute("select * from market.usdjpy_as_of(%s)", (knowledge_cutoff,))
            row = cur.fetchone()
        if row is None or row[1] is None:
            return None
        return FxObservation(
            rate=Decimal(row[1]), source_date=row[0], observed_at=row[6], available_at=row[7]
        )

    def write_fx(self, params: dict) -> None:
        with self._connection.cursor() as cur:
            cur.execute(INSERT_FX, params)

    def write_eligibility(
        self,
        results: list[tuple[str, EligibilityResult]],
        *,
        run_id: str,
        market_code: str,
        provider_id: str,
        as_of_date: date,
        knowledge_cutoff: datetime,
        universe_run_id: str | None = None,
        market_data_run_id: str | None = None,
        fx_run_id: str | None = None,
        universe_decisions: dict[str, str] | None = None,
        security_ids: dict[str, str] | None = None,
    ) -> int:
        universe_decisions = universe_decisions or {}
        security_ids = security_ids or {}

        with self._connection.cursor() as cur:
            for native_symbol, result in results:
                cur.execute(
                    INSERT_ELIGIBILITY,
                    {
                        "run_id": run_id, "market_code": market_code, "provider_id": provider_id,
                        "native_symbol": native_symbol, "as_of_date": as_of_date,
                        "security_id": security_ids.get(native_symbol),
                        "exchange_code": None, "identity_version": None,
                        "universe_decision": universe_decisions.get(native_symbol),
                        "decision": str(result.decision), "rule_version": result.rule_version,
                        "price": result.price, "price_currency": result.price_currency,
                        "price_basis": None if result.price_basis is None else str(result.price_basis),
                        "price_trade_date": result.price_trade_date,
                        "price_observed_at": result.price_observed_at,
                        "price_available_at": result.price_available_at,
                        "price_age_days": result.price_age_days,
                        "fx_rate": result.fx_rate, "fx_source_date": result.fx_source_date,
                        "fx_observed_at": result.fx_observed_at,
                        "fx_available_at": result.fx_available_at,
                        "fx_age_seconds": result.fx_age_seconds,
                        "converted_jpy": result.converted_jpy,
                        "universe_run_id": universe_run_id,
                        "market_data_run_id": market_data_run_id,
                        "fx_run_id": fx_run_id,
                        "knowledge_cutoff": knowledge_cutoff,
                    },
                )
        return len(results)

    # ------------------------------------------------------------ stage 1
    def write_features(
        self,
        features: list[DailyFeatures],
        *,
        run_id: str,
        observed_at: datetime,
        available_at: datetime,
        availability_basis: str = "OBSERVED_NOW",
    ) -> int:
        if not features:
            return 0

        columns = [
            "run_id", "market_code", "provider_id", "native_symbol", "trade_date",
            "feature_version", "series_basis", "bars_available", "warmup_satisfied",
            "observed_at", "available_at", "availability_basis",
        ]
        numeric = [
            name
            for name in features[0].as_dict()
            if name not in {
                "market_code", "provider_id", "native_symbol", "trade_date",
                "feature_version", "series_basis", "bars_available", "warmup_satisfied",
            }
        ]
        columns.extend(numeric)
        placeholders = ", ".join(f"%({name})s" for name in columns)
        statement = (
            f"insert into screening.features_daily ({', '.join(columns)}) values ({placeholders})"
        )

        with self._connection.cursor() as cur:
            for row in features:
                params = row.as_dict()
                params |= {
                    "run_id": run_id,
                    "observed_at": observed_at,
                    "available_at": available_at,
                    "availability_basis": availability_basis,
                }
                cur.execute(statement, params)
        return len(features)

    def write_candidates(
        self,
        candidates: list[CandidateResult],
        *,
        run_id: str,
        observed_at: datetime,
        available_at: datetime,
        availability_basis: str = "OBSERVED_NOW",
        price_eligible: dict[str, bool] | None = None,
        universe_decisions: dict[str, str] | None = None,
    ) -> int:
        import json  # noqa: PLC0415 - only needed here

        price_eligible = price_eligible or {}
        universe_decisions = universe_decisions or {}
        written = 0

        with self._connection.cursor() as cur:
            for candidate in candidates:
                if not candidate.is_candidate:
                    continue
                cur.execute(
                    """
                    insert into screening.route_candidates (
                      run_id, market_code, provider_id, native_symbol, trade_date,
                      route_version, feature_version, discovery_routes, route_evidence, route_count,
                      price_eligible, universe_decision,
                      observed_at, available_at, availability_basis
                    ) values (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        run_id, candidate.market_code, candidate.provider_id,
                        candidate.native_symbol, candidate.trade_date,
                        candidate.route_version, candidate.feature_version,
                        candidate.discovery_routes, json.dumps(candidate.route_evidence),
                        candidate.route_count,
                        price_eligible.get(candidate.native_symbol),
                        universe_decisions.get(candidate.native_symbol),
                        observed_at, available_at, availability_basis,
                    ),
                )
                written += 1
        return written
