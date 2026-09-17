"""The end-to-end chain: universe through to setup, watch or reject.

The properties under test are the ones that would be easy to lose in wiring:
the union really is a union, a security nominated only by news is not dropped for
having no chart signal, and the report says out loud that it ran on fixtures.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from surge.analysis import Stage3State
from surge.licensing import AvailabilityBasis
from surge.market.eligibility import FxObservation
from surge.market.models import CanonicalBar, PriceBasis
from surge.material.models import (
    EntityRelation,
    EventSecurityFeatures,
    EventSource,
    LinkConfidence,
    MaterialEvent,
    RelationType,
    SourceRole,
)
from surge.pipeline import EodPipeline, StageStatus

T0 = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 17, 6, 30, tzinfo=UTC)
CANONICAL_SHA = "a" * 64


def _bars(closes, *, symbol, market="JP", volumes=None, start=date(2026, 1, 5)):
    bars = []
    day = start
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(
            CanonicalBar(
                provider_id="fixture",
                dataset_key="FIXTURE_EOD",
                market_code=market,
                native_symbol=symbol,
                trade_date=day,
                currency="JPY" if market == "JP" else "USD",
                open=Decimal(str(close)),
                high=Decimal(str(close * 1.02)),
                low=Decimal(str(close * 0.98)),
                close=Decimal(str(close)),
                volume=Decimal(str(volumes[index] if volumes else 100000)),
                open_basis=PriceBasis.RAW,
                high_basis=PriceBasis.RAW,
                low_basis=PriceBasis.RAW,
                close_basis=PriceBasis.RAW,
                volume_basis=PriceBasis.RAW,
                observed_at=T0,
                available_at=T0,
                availability_basis=AvailabilityBasis.OBSERVED_NOW,
            )
        )
        day += timedelta(days=1)
    return bars


def _as_of(bars):
    return bars[-1].trade_date


def _material_evaluation(security_id="sec-news", event_key="evt-1"):
    event = MaterialEvent(
        event_key=event_key,
        event_type="EARNINGS_REVISION",
        scope="JP",
        merge_version="event-merge-1.0.0",
        sources=(
            EventSource(document_id="d1", source_key="tdnet", role=SourceRole.DISCOVERY, available_to_model_at=T0),
        ),
        event_id=event_key,
    )
    relation = EntityRelation(
        relation_type=RelationType.DIRECT_COMPANY,
        confidence=LinkConfidence.STRONG,
        extractor="fixture",
        extractor_version="1.0.0",
        security_id=security_id,
    )
    features = EventSecurityFeatures(
        event_id=event_key,
        security_id=security_id,
        feature_version="material-feature-1.0.0",
        knowledge_cutoff=CUTOFF,
        novelty=0.9,
        methods={"novelty": "fixture"},
    )
    return event, [(event, [relation], features, ("TIMELY_DISCLOSURE",))]


def _run(pipeline=None, **overrides):
    rising = [800 + n * 4 for n in range(60)]
    bars = {"13010": _bars(rising, symbol="13010")}
    kwargs = {
        "market_code": "JP",
        "as_of_date": _as_of(bars["13010"]),
        "knowledge_cutoff": CUTOFF,
        "bars_by_symbol": bars,
        "security_ids": {"13010": "sec-chart"},
        "fx": FxObservation(rate=Decimal("150"), source_date=date(2026, 9, 16), observed_at=T0, available_at=T0,
                             provider_id="fixture"),
        "canonical_text": "CANONICAL BODY",
        "canonical_prompt_sha256": CANONICAL_SHA,
    }
    kwargs.update(overrides)
    return (pipeline or EodPipeline()).run(**kwargs)


# ------------------------------------------------------------------ the chain


def test_the_chain_connects_from_universe_to_a_state():
    report = _run()

    assert report.eligibility is not None
    assert report.screening is not None
    assert report.stage3 is not None
    assert report.stage3.results
    assert report.stage3.results[0].state in set(Stage3State)


def test_the_report_says_it_ran_on_fixtures():
    """The most comfortable lie available is "the pipeline works"."""

    report = _run()
    assert report.data_is_fixture is True
    assert report.ran_on_real_data is False
    ran = [s for s in report.stage_status.values() if s is not StageStatus.SKIPPED_NO_INPUT]
    assert ran and all(status is StageStatus.FIXTURE_ONLY for status in ran)
    assert any("demonstrates nothing about any security" in note for note in report.notes)


def test_a_security_with_only_news_survives_the_union():
    """The point of running the two sides independently."""

    event, evaluation = _material_evaluation(security_id="sec-news")
    report = _run(material_evaluations={"sec-news": evaluation}, events_by_id={event.event_key: event})

    origins = {candidate.security_id: candidate.origin for candidate in report.union}
    assert origins.get("sec-news") == "MATERIAL_ONLY"
    assert report.summary["union_by_origin"]["MATERIAL_ONLY"] == 1


def test_a_security_with_only_a_chart_signal_survives_the_union():
    report = _run()
    origins = {candidate.origin for candidate in report.union}
    assert origins == {"TECHNICAL_ONLY"}


def test_a_security_both_sides_found_is_marked_as_such():
    event, evaluation = _material_evaluation(security_id="sec-chart")
    report = _run(material_evaluations={"sec-chart": evaluation}, events_by_id={event.event_key: event})

    origins = {candidate.security_id: candidate.origin for candidate in report.union}
    assert origins["sec-chart"] == "BOTH"
    candidate = next(c for c in report.union if c.security_id == "sec-chart")
    assert candidate.technical_routes
    assert candidate.material_routes == ["M1"]


def test_a_news_only_candidate_with_no_price_series_is_noted_not_silently_dropped():
    event, evaluation = _material_evaluation(security_id="sec-news")
    report = _run(material_evaluations={"sec-news": evaluation}, events_by_id={event.event_key: event})

    assert any("has no price series" in note for note in report.notes)
    # It still reaches Stage 3: an event we cannot chart is still an event.
    assert any(result.security_id == "sec-news" for result in report.stage3.results)


def test_stage2_measures_only_the_union():
    report = _run()
    assessed = {assessment.security_id for assessment in report.stage2}
    assert assessed <= {candidate.security_id for candidate in report.union}


def test_obstacles_are_carried_into_stage3():
    report = _run()
    candidate = report.union[0]
    bundle = report.stage3.results[0].bundle
    assert "price_obstacles" in bundle.sections
    assert candidate.security_id in report.obstacles


def test_missing_material_input_is_a_gap_not_a_finding():
    report = _run()
    assert report.stage_status["material_routes"] is StageStatus.SKIPPED_NO_INPUT
    assert any("gap in the inputs rather than a finding" in note for note in report.notes)


def test_no_bars_skips_the_price_stages_without_failing():
    report = _run(bars_by_symbol={}, security_ids={})
    assert report.stage_status["eligibility"] is StageStatus.SKIPPED_NO_INPUT
    assert report.stage_status["screening"] is StageStatus.SKIPPED_NO_INPUT
    assert report.union == []


def test_stage3_will_not_run_without_the_canonical_hash():
    """A bundle that cannot say which instructions applied is not reproducible."""

    report = _run(canonical_prompt_sha256="")
    assert report.stage_status["stage3"] is StageStatus.SKIPPED_NO_INPUT
    assert report.stage3 is None
    assert any("canonical prompt hash" in note for note in report.notes)


def test_the_summary_reports_every_stage():
    event, evaluation = _material_evaluation(security_id="sec-news")
    summary = _run(
        material_evaluations={"sec-news": evaluation}, events_by_id={event.event_key: event}
    ).summary

    assert summary["market_code"] == "JP"
    assert summary["data_is_fixture"] is True
    assert set(summary["union_by_origin"]) == {"TECHNICAL_ONLY", "MATERIAL_ONLY", "BOTH"}
    assert "stage3" in summary
    assert "stage_status" in summary


def test_the_price_filter_still_applies_inside_the_chain():
    """Nothing above 3,000 JPY reaches the technical side."""

    expensive = [3200 + n for n in range(60)]
    bars = {"99990": _bars(expensive, symbol="99990")}
    report = _run(
        bars_by_symbol=bars,
        security_ids={"99990": "sec-expensive"},
        as_of_date=_as_of(bars["99990"]),
    )

    assert report.eligibility.eligible_symbols == set()
    assert report.screening.features == []
    assert report.union == []
