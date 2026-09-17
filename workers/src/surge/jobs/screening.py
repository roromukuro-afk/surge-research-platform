"""The two jobs that turn stored prices into decisions.

``PriceEligibilityJob`` applies the 3,000 JPY rule across a universe and records
every outcome, including the ones where the rule could not be applied.

``Stage1ScreeningJob`` builds a comparable series per security, computes the
feature row, and runs Routes A-H over it.

Both are deliberately given their inputs rather than fetching them. A job that
reaches out to storage mid-calculation is a job you cannot test, cannot replay
and cannot reason about the knowledge cutoff of; passing the bars in makes the
cutoff an argument instead of an assumption.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from surge.features.engine import FEATURE_VERSION, DailyFeatures, FeatureError, compute_features
from surge.market.eligibility import (
    DEFAULT_RULE,
    EligibilityDecision,
    EligibilityResult,
    FilterRule,
    FxObservation,
    evaluate,
    summarise,
)
from surge.market.models import CanonicalAction, CanonicalBar
from surge.market.series import SeriesError, build_comparable_series, latest_price
from surge.routes.engine import ROUTE_VERSION, CandidateResult, evaluate_routes, route_summary

ELIGIBILITY_JOB_VERSION = "price-eligibility-1.0.0"
SCREENING_JOB_VERSION = "stage1-screening-1.0.0"


class EligibilityWriter(Protocol):
    def write_eligibility(self, results, **kwargs) -> int: ...


class ScreeningWriter(Protocol):
    def write_features(self, features, **kwargs) -> int: ...

    def write_candidates(self, candidates, **kwargs) -> int: ...


@dataclass
class EligibilityReport:
    market_code: str
    as_of_date: date
    knowledge_cutoff: datetime
    rule_version: str
    results: list[tuple[str, EligibilityResult]] = field(default_factory=list)
    written: int = 0

    @property
    def summary(self) -> dict[str, int]:
        return summarise([result for _symbol, result in self.results])

    @property
    def eligible_symbols(self) -> set[str]:
        return {
            symbol
            for symbol, result in self.results
            if result.decision is EligibilityDecision.PRICE_ELIGIBLE
        }

    def as_dict(self) -> dict:
        return {
            "market_code": self.market_code,
            "as_of_date": self.as_of_date.isoformat(),
            "knowledge_cutoff": self.knowledge_cutoff.isoformat(),
            "rule_version": self.rule_version,
            "written": self.written,
            "summary": self.summary,
        }


@dataclass
class Stage1Report:
    market_code: str
    as_of_date: date
    feature_version: str = FEATURE_VERSION
    route_version: str = ROUTE_VERSION
    features: list[DailyFeatures] = field(default_factory=list)
    candidates: list[CandidateResult] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    features_written: int = 0
    candidates_written: int = 0

    @property
    def summary(self) -> dict[str, int]:
        counts = route_summary(self.candidates)
        counts["securities_with_features"] = len(self.features)
        counts["securities_refused"] = len(self.refused)
        counts["warmup_incomplete"] = sum(1 for f in self.features if not f.warmup_satisfied)
        return counts

    def as_dict(self) -> dict:
        return {
            "market_code": self.market_code,
            "as_of_date": self.as_of_date.isoformat(),
            "feature_version": self.feature_version,
            "route_version": self.route_version,
            "features_written": self.features_written,
            "candidates_written": self.candidates_written,
            "summary": self.summary,
            "refused": self.refused[:20],
        }


class PriceEligibilityJob:
    """Applies the 3,000 JPY rule across one market for one date."""

    job_name = "price_eligibility"
    job_version = ELIGIBILITY_JOB_VERSION

    def __init__(
        self,
        *,
        writer: EligibilityWriter | None = None,
        rule: FilterRule = DEFAULT_RULE,
        run_id: str | None = None,
    ) -> None:
        self._writer = writer
        self._rule = rule
        self._run_id = run_id

    def run(
        self,
        *,
        market_code: str,
        as_of_date: date,
        knowledge_cutoff: datetime,
        bars_by_symbol: Mapping[str, Sequence[CanonicalBar]],
        fx: FxObservation | None = None,
        universe_decisions: Mapping[str, str] | None = None,
        provider_id: str = "unknown",
        universe_run_id: str | None = None,
        market_data_run_id: str | None = None,
        fx_run_id: str | None = None,
        security_ids: Mapping[str, str] | None = None,
    ) -> EligibilityReport:
        report = EligibilityReport(
            market_code=market_code,
            as_of_date=as_of_date,
            knowledge_cutoff=knowledge_cutoff,
            rule_version=self._rule.rule_version,
        )

        for symbol in sorted(bars_by_symbol):
            bars = [
                bar
                for bar in bars_by_symbol[symbol]
                # Nothing the cutoff could not know reaches the rule. Belt and
                # braces: evaluate() checks this too, and both are cheap.
                if bar.available_at is None or bar.available_at <= knowledge_cutoff
            ]
            bar = latest_price(list(bars), as_of=as_of_date) if bars else None
            report.results.append(
                (
                    symbol,
                    evaluate(
                        market_code=market_code,
                        as_of_date=as_of_date,
                        knowledge_cutoff=knowledge_cutoff,
                        bar=bar,
                        fx=fx,
                        rule=self._rule,
                    ),
                )
            )

        if self._writer is not None and self._run_id is not None:
            report.written = self._writer.write_eligibility(
                report.results,
                run_id=self._run_id,
                market_code=market_code,
                provider_id=provider_id,
                as_of_date=as_of_date,
                knowledge_cutoff=knowledge_cutoff,
                universe_run_id=universe_run_id,
                market_data_run_id=market_data_run_id,
                fx_run_id=fx_run_id,
                universe_decisions=dict(universe_decisions or {}),
                security_ids=dict(security_ids or {}),
            )

        return report


class Stage1ScreeningJob:
    """Features and Routes A-H over one market for one date."""

    job_name = "stage1_screening"
    job_version = SCREENING_JOB_VERSION

    def __init__(self, *, writer: ScreeningWriter | None = None, run_id: str | None = None) -> None:
        self._writer = writer
        self._run_id = run_id

    def run(
        self,
        *,
        market_code: str,
        as_of_date: date,
        bars_by_symbol: Mapping[str, Sequence[CanonicalBar]],
        actions_by_symbol: Mapping[str, Sequence[CanonicalAction]] | None = None,
        observed_at: datetime,
        available_at: datetime,
        price_eligible: Mapping[str, bool] | None = None,
        universe_decisions: Mapping[str, str] | None = None,
        screen_only_eligible: bool = True,
    ) -> Stage1Report:
        """Screen the universe.

        ``screen_only_eligible`` keeps the default behaviour honest: a security
        the 3,000 JPY rule excluded cannot become a prediction, so computing its
        candidacy would be work whose result may never be used. It is a flag
        rather than a hard rule because research legitimately wants the other
        population - measuring what the filter costs needs the excluded set too.
        """

        actions_by_symbol = actions_by_symbol or {}
        price_eligible = price_eligible or {}
        report = Stage1Report(market_code=market_code, as_of_date=as_of_date)

        for symbol in sorted(bars_by_symbol):
            if screen_only_eligible and price_eligible and not price_eligible.get(symbol, False):
                continue

            try:
                series = build_comparable_series(
                    list(bars_by_symbol[symbol]),
                    list(actions_by_symbol.get(symbol, [])),
                    as_of=as_of_date,
                )
            except SeriesError as exc:
                report.refused.append((symbol, f"series: {exc}"))
                continue

            try:
                features = compute_features(series)
            except FeatureError as exc:
                report.refused.append((symbol, f"features: {exc}"))
                continue

            report.features.append(features)
            candidate = evaluate_routes(features, series=series)
            if candidate.is_candidate:
                report.candidates.append(candidate)

        if self._writer is not None and self._run_id is not None:
            report.features_written = self._writer.write_features(
                report.features,
                run_id=self._run_id,
                observed_at=observed_at,
                available_at=available_at,
            )
            report.candidates_written = self._writer.write_candidates(
                report.candidates,
                run_id=self._run_id,
                observed_at=observed_at,
                available_at=available_at,
                price_eligible=dict(price_eligible),
                universe_decisions=dict(universe_decisions or {}),
            )

        return report
