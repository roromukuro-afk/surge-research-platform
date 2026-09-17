"""The end-of-day pipeline: universe in, setup or watch or reject out.

This is the chain the project has been building toward:

    universe -> price eligibility -> features -> Routes A-H
                                              \\
                 news -> events -> relations -> Material Routes M1-M6
                                              /
                                         union -> Stage 2 -> Stage 3

Two properties matter more than the plumbing.

**The union is a union.** The technical and material sides run independently and
neither filters the other. A security with a chart setup and no news survives; so
does one with news and no chart setup. Intersecting them would be an AND filter
wearing a union's name, and would discard exactly the cases the two-route design
exists to catch.

**Every stage says what it ran on.** A stage that ran against fixtures because no
provider is contracted is not a stage that ran. The report carries a status per
stage, so "the pipeline works" never quietly comes to mean "the pipeline ran on
data we invented".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from surge.analysis.stage3 import Stage3Job, Stage3Report
from surge.chart.obstacles import find_obstacles
from surge.chart.stage2 import Stage2Assessment, assess
from surge.jobs.screening import EligibilityReport, PriceEligibilityJob, Stage1Report, Stage1ScreeningJob
from surge.market.eligibility import DEFAULT_RULE, FilterRule, FxObservation
from surge.market.models import CanonicalAction, CanonicalBar
from surge.market.series import SeriesError, build_comparable_series
from surge.material.models import MaterialEvent
from surge.material.routes import MATERIAL_ROUTE_VERSION, MaterialCandidate, build_candidate

EOD_PIPELINE_VERSION = "eod-pipeline-1.0.0"


class StageStatus(StrEnum):
    """What a stage actually did.

    ``FIXTURE_ONLY`` is the honest answer for most of this pipeline today: the
    code is complete and no contracted price provider exists yet, so the inputs
    are synthetic. Calling that RAN would make the report lie in the most
    comfortable direction.
    """

    RAN = "RAN"
    FIXTURE_ONLY = "FIXTURE_ONLY"
    SKIPPED_NO_INPUT = "SKIPPED_NO_INPUT"
    SKIPPED_NO_CREDENTIAL = "SKIPPED_NO_CREDENTIAL"
    FAILED = "FAILED"


@dataclass
class UnionCandidate:
    """One security, and which side or sides nominated it."""

    security_id: str
    market_code: str
    technical_routes: list[str] = field(default_factory=list)
    material_routes: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        if self.technical_routes and self.material_routes:
            return "BOTH"
        return "TECHNICAL_ONLY" if self.technical_routes else "MATERIAL_ONLY"


@dataclass
class EodReport:
    as_of_date: date
    market_code: str
    knowledge_cutoff: datetime
    pipeline_version: str = EOD_PIPELINE_VERSION
    data_is_fixture: bool = True

    eligibility: EligibilityReport | None = None
    screening: Stage1Report | None = None
    material_candidates: list[MaterialCandidate] = field(default_factory=list)
    union: list[UnionCandidate] = field(default_factory=list)
    stage2: list[Stage2Assessment] = field(default_factory=list)
    obstacles: dict = field(default_factory=dict)
    stage3: Stage3Report | None = None

    stage_status: dict[str, StageStatus] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        origins = {"TECHNICAL_ONLY": 0, "MATERIAL_ONLY": 0, "BOTH": 0}
        for candidate in self.union:
            origins[candidate.origin] += 1
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "market_code": self.market_code,
            "data_is_fixture": self.data_is_fixture,
            "analysis_is_a_stand_in": self.analysis_is_a_stand_in,
            "eligible": len(self.eligibility.eligible_symbols) if self.eligibility else 0,
            "technical_candidates": len(self.screening.candidates) if self.screening else 0,
            "material_candidates": len(self.material_candidates),
            "union": len(self.union),
            "union_by_origin": origins,
            "stage2_assessed": len(self.stage2),
            "stage3": self.stage3.summary if self.stage3 else {},
            "stage_status": {stage: status.value for stage, status in self.stage_status.items()},
        }

    @property
    def analysis_is_a_stand_in(self) -> bool:
        """Whether the day's verdicts came from the deterministic mock.

        Separate from the data question. A run can have real prices, real
        disclosures and a rule-based stand-in producing the states, and calling
        that "live" would be the most flattering reading available.
        """

        if self.stage3 is None:
            return False
        return any(
            result.response.provider_kind.value == "DETERMINISTIC_MOCK"
            for result in self.stage3.results
        )

    @property
    def ran_on_real_data(self) -> bool:
        """True only when no stage was satisfied with fixtures or a stand-in.

        Deliberately conservative on both axes. Reading this as "the platform is
        live" should require every stage to have seen real data AND a real model
        to have produced the analysis - otherwise a run with real prices and a
        rule-based stand-in would read as a live day.
        """

        return (
            not self.data_is_fixture
            and not self.analysis_is_a_stand_in
            and all(status is StageStatus.RAN for status in self.stage_status.values())
        )


class EodPipeline:
    """Runs the end-of-day chain for one market on one date."""

    job_name = "eod_pipeline"
    job_version = EOD_PIPELINE_VERSION

    def __init__(
        self,
        *,
        rule: FilterRule = DEFAULT_RULE,
        stage3: Stage3Job | None = None,
    ) -> None:
        self._rule = rule
        self._stage3 = stage3 or Stage3Job()

    def run(
        self,
        *,
        market_code: str,
        as_of_date: date,
        knowledge_cutoff: datetime,
        bars_by_symbol: Mapping[str, Sequence[CanonicalBar]],
        security_ids: Mapping[str, str],
        actions_by_symbol: Mapping[str, Sequence[CanonicalAction]] | None = None,
        fx: FxObservation | None = None,
        material_evaluations: Mapping[str, Sequence] | None = None,
        events_by_id: Mapping[str, MaterialEvent] | None = None,
        canonical_text: str = "",
        canonical_prompt_sha256: str = "",
        addenda_texts: Sequence[str] = (),
        addenda_sha256: Sequence[str] = (),
        data_is_fixture: bool = True,
    ) -> EodReport:
        """Run the chain.

        ``material_evaluations`` maps a security id to the argument tuples
        :func:`surge.material.routes.build_candidate` expects. It is separate from
        the price inputs because the two sides genuinely do not depend on each
        other - which is the property the union relies on.
        """

        report = EodReport(
            as_of_date=as_of_date,
            market_code=market_code,
            knowledge_cutoff=knowledge_cutoff,
            data_is_fixture=data_is_fixture,
        )
        if data_is_fixture:
            report.notes.append(
                "inputs are fixtures: no contracted price provider exists yet (D-102, D-103), so this run "
                "demonstrates that the chain connects and demonstrates nothing about any security"
            )

        actions_by_symbol = actions_by_symbol or {}
        ran = StageStatus.FIXTURE_ONLY if data_is_fixture else StageStatus.RAN

        # --- 1. the 3,000 JPY hard filter ----------------------------------
        if not bars_by_symbol:
            report.stage_status["eligibility"] = StageStatus.SKIPPED_NO_INPUT
            report.stage_status["screening"] = StageStatus.SKIPPED_NO_INPUT
        else:
            report.eligibility = PriceEligibilityJob(rule=self._rule).run(
                market_code=market_code,
                as_of_date=as_of_date,
                knowledge_cutoff=knowledge_cutoff,
                bars_by_symbol=bars_by_symbol,
                fx=fx,
                security_ids=security_ids,
            )
            report.stage_status["eligibility"] = ran

        # --- 2. Stage 1: features and Routes A-H ---------------------------
        if report.eligibility is not None:
            eligible = report.eligibility.eligible_symbols
            report.screening = Stage1ScreeningJob().run(
                market_code=market_code,
                as_of_date=as_of_date,
                bars_by_symbol=bars_by_symbol,
                actions_by_symbol=actions_by_symbol,
                observed_at=knowledge_cutoff,
                available_at=knowledge_cutoff,
                price_eligible={symbol: symbol in eligible for symbol in bars_by_symbol},
            )
            report.stage_status["screening"] = ran

        # --- 3. the material side, run independently -----------------------
        if material_evaluations:
            for security_id, evaluations in material_evaluations.items():
                candidate = build_candidate(security_id, evaluations)
                if candidate.is_candidate:
                    report.material_candidates.append(candidate)
            report.stage_status["material_routes"] = ran
        else:
            report.stage_status["material_routes"] = StageStatus.SKIPPED_NO_INPUT
            report.notes.append(
                "no material evaluations supplied, so the material side nominated nobody. That is a gap "
                "in the inputs rather than a finding about the day"
            )

        # --- 4. union ------------------------------------------------------
        report.union = self._union(report, security_ids, market_code)

        # --- 5. Stage 2 on the union ---------------------------------------
        by_security = {security_ids[symbol]: symbol for symbol in bars_by_symbol if symbol in security_ids}
        for candidate in report.union:
            symbol = by_security.get(candidate.security_id)
            if symbol is None:
                report.notes.append(
                    f"{candidate.security_id} was nominated by the material side but has no price series; "
                    "Stage 2 cannot measure a chart that is not there"
                )
                continue
            try:
                series = build_comparable_series(
                    list(bars_by_symbol[symbol]),
                    list(actions_by_symbol.get(symbol, [])),
                    as_of=as_of_date,
                )
            except SeriesError as exc:
                report.notes.append(f"{symbol}: {exc}")
                continue
            assessment = assess(series, security_id=candidate.security_id)
            report.stage2.append(assessment)
            report.obstacles[candidate.security_id] = find_obstacles(
                series, security_id=candidate.security_id
            )
        report.stage_status["stage2"] = ran if report.stage2 else StageStatus.SKIPPED_NO_INPUT

        # --- 6. Stage 3 ----------------------------------------------------
        if not canonical_prompt_sha256:
            report.stage_status["stage3"] = StageStatus.SKIPPED_NO_INPUT
            report.notes.append(
                "Stage 3 needs the canonical prompt hash; without it the bundle could not record what "
                "instructions the answer was formed under"
            )
            return report

        stage2_by_security = {a.security_id: a for a in report.stage2}
        candidates = []
        for candidate in report.union:
            assessment = stage2_by_security.get(candidate.security_id)
            obstacles = report.obstacles.get(candidate.security_id)
            candidates.append(
                {
                    "security_id": candidate.security_id,
                    "market_code": candidate.market_code,
                    "routes": {
                        "technical_routes": candidate.technical_routes,
                        "material_routes": candidate.material_routes,
                    },
                    "stage2": _stage2_section(assessment),
                    "price_obstacles": (
                        [obstacle.as_dict() for obstacle in obstacles.obstacles] if obstacles else []
                    ),
                    "materials": _materials_section(candidate, events_by_id or {}),
                    "threshold_reference_price": assessment.close if assessment else None,
                    "threshold_reference_kind": "EOD_CLOSE",
                }
            )

        report.stage3 = self._stage3.run(
            as_of_date=as_of_date,
            knowledge_cutoff=knowledge_cutoff,
            canonical_text=canonical_text,
            canonical_prompt_sha256=canonical_prompt_sha256,
            candidates=candidates,
            addenda_texts=addenda_texts,
            addenda_sha256=addenda_sha256,
            versions={
                "route_version": report.screening.route_version if report.screening else None,
                "feature_version": report.screening.feature_version if report.screening else None,
                "material_route_version": MATERIAL_ROUTE_VERSION,
                "stage2_version": report.stage2[0].assessment_version if report.stage2 else None,
                "concept_version": report.stage2[0].concept_version if report.stage2 else None,
            },
        )
        report.stage_status["stage3"] = ran if report.stage3.results else StageStatus.SKIPPED_NO_INPUT
        return report

    @staticmethod
    def _union(report: EodReport, security_ids, market_code: str) -> list[UnionCandidate]:
        """Full outer union. Neither side may remove the other's candidates."""

        merged: dict[str, UnionCandidate] = {}

        if report.screening is not None:
            for candidate in report.screening.candidates:
                security_id = security_ids.get(candidate.native_symbol, candidate.native_symbol)
                merged[security_id] = UnionCandidate(
                    security_id=security_id,
                    market_code=market_code,
                    technical_routes=list(candidate.discovery_routes),
                )

        for candidate in report.material_candidates:
            entry = merged.get(candidate.security_id)
            if entry is None:
                entry = UnionCandidate(security_id=candidate.security_id, market_code=market_code)
                merged[candidate.security_id] = entry
            entry.material_routes = list(candidate.discovery_routes)
            entry.event_ids = list(candidate.event_ids)

        return [merged[key] for key in sorted(merged)]


def _stage2_section(assessment: Stage2Assessment | None) -> dict | None:
    if assessment is None:
        return None
    return {
        "close": assessment.close,
        "atr_pct": assessment.atr_pct,
        "nearest_resistance": assessment.nearest_resistance,
        "nearest_support": assessment.nearest_support,
        "relative_volume_20d": assessment.relative_volume_20d,
        "concepts_fired": list(assessment.concepts_fired),
        "measurement_gaps": list(assessment.measurement_gaps),
        "intraday_available": assessment.intraday_available,
    }


def _materials_section(candidate: UnionCandidate, events_by_id: Mapping[str, MaterialEvent]) -> list[dict]:
    materials = []
    for event_id in candidate.event_ids:
        event = events_by_id.get(event_id)
        materials.append(
            {
                "event_id": event_id,
                "event_type": event.event_type if event else None,
                "first_known_at": event.first_known_at.isoformat() if event else None,
                "independent_sources": event.independent_source_count if event else None,
                # Whether the material landed after the close decides whether this
                # is a chart setup or a catalyst setup, and the two are kept apart
                # so post-close news is never read as if the close had priced it.
                "after_close": bool(event and _after_close(event)),
            }
        )
    return materials


def _after_close(event: MaterialEvent) -> bool:
    """Whether the event became knowable after the session closed.

    Currently a placeholder that reads a flag the caller may set, because a real
    answer needs a trading calendar and this project does not have a verified one
    yet. Returning False by default is the safe direction: it treats material as
    a chart setup rather than claiming the close had not priced it.
    """

    return bool(getattr(event, "after_close", False))
