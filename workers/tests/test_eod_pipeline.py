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
        # The universe rule is global. Without a read, the gate promotes nothing -
        # so a test about routes has to supply one or it is testing the gate.
        "universe": {
            "sec-chart": ("INCLUDED", "TARGET_MARKET_COMMON_STOCK"),
            "sec-news": ("INCLUDED", "TARGET_MARKET_COMMON_STOCK"),
            "sec-expensive": ("INCLUDED", "TARGET_MARKET_COMMON_STOCK"),
        },
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


def test_a_stand_in_analysis_is_never_a_live_run():
    """Real prices plus a rule-based analysis is not a live day.

    Without this, a run with every stage on real data and the deterministic
    stand-in producing the states would report ran_on_real_data - the most
    flattering reading available, and the wrong one.
    """

    report = _run(data_is_fixture=False)

    assert report.analysis_is_a_stand_in is True
    assert report.ran_on_real_data is False
    assert report.summary["analysis_is_a_stand_in"] is True


def test_the_data_question_and_the_model_question_are_separate():
    report = _run()
    assert report.data_is_fixture is True
    assert report.analysis_is_a_stand_in is True
    # Both must clear before the run counts as live, and they are reported apart
    # so it is visible which one is outstanding.
    assert "data_is_fixture" in report.summary
    assert "analysis_is_a_stand_in" in report.summary


# ------------------------------------------------------------ the universe gate


def test_an_excluded_security_is_stored_and_not_promoted():
    """An ETF's disclosure is a real disclosure and a real record.

    It is not something this platform predicts on, and the universe rule says so
    for the material side exactly as it does for the technical one - otherwise a
    fund would reach Stage 3 through a filing while being excluded from every
    price screen.
    """

    event, evaluation = _material_evaluation(security_id="sec-etf")
    report = _run(
        material_evaluations={"sec-etf": evaluation},
        events_by_id={event.event_key: event},
        universe={"sec-etf": ("EXCLUDED", "ETF"), "sec-chart": ("INCLUDED", "TARGET_MARKET_COMMON_STOCK")},
    )

    assert report.material_candidates == []
    assert [c.security_id for c in report.material_held_by_universe] == ["sec-etf"]
    assert "sec-etf" not in {candidate.security_id for candidate in report.union}
    verdict = report.universe_gate.verdicts["sec-etf"]
    assert verdict.decision.value == "EXCLUDED"
    assert verdict.research_visible is True
    assert verdict.formal_candidate is False


def test_an_unresolved_security_is_research_visible_but_not_a_formal_candidate():
    """UNRESOLVED is not a synonym for excluded. Nothing is discarded; it is
    simply not promoted while the question is open."""

    event, evaluation = _material_evaluation(security_id="sec-open")
    report = _run(
        material_evaluations={"sec-open": evaluation},
        events_by_id={event.event_key: event},
        universe={"sec-open": ("UNRESOLVED", "FOREIGN_STOCK_RULE_PENDING")},
    )

    verdict = report.universe_gate.verdicts["sec-open"]
    assert verdict.decision.value == "UNRESOLVED"
    assert verdict.research_visible is True
    assert verdict.formal_candidate is False
    assert [c.security_id for c in report.material_held_by_universe] == ["sec-open"]
    assert "sec-open" not in {candidate.security_id for candidate in report.union}


def test_an_included_security_is_promoted():
    event, evaluation = _material_evaluation(security_id="sec-news")
    report = _run(material_evaluations={"sec-news": evaluation}, events_by_id={event.event_key: event})

    assert [c.security_id for c in report.material_candidates] == ["sec-news"]
    assert report.material_held_by_universe == []
    assert "sec-news" in {candidate.security_id for candidate in report.union}


def test_a_security_the_snapshot_has_never_heard_of_is_held_open_not_excluded():
    """A listing newer than the snapshot is an unanswered question.

    Deciding it the convenient way would quietly delete new issuers, which is
    the population most likely to move.
    """

    event, evaluation = _material_evaluation(security_id="sec-brand-new")
    report = _run(
        material_evaluations={"sec-brand-new": evaluation},
        events_by_id={event.event_key: event},
        universe={"sec-chart": ("INCLUDED", "TARGET_MARKET_COMMON_STOCK")},
    )

    verdict = report.universe_gate.verdicts["sec-brand-new"]
    assert verdict.decision.value == "UNRESOLVED"
    assert verdict.reason_code == "NOT_IN_UNIVERSE_SNAPSHOT"
    assert verdict.formal_candidate is False


def test_no_universe_read_promotes_nothing_and_says_why():
    """A missing input, not a finding about the day."""

    event, evaluation = _material_evaluation(security_id="sec-news")
    report = _run(
        material_evaluations={"sec-news": evaluation},
        events_by_id={event.event_key: event},
        universe=None,
    )

    assert report.material_candidates == []
    assert any("missing input, not a finding" in note for note in report.notes)


def test_the_technical_side_is_not_filtered_by_the_material_gate():
    """The two routes stay independent. The gate is a global rule, not a route."""

    event, evaluation = _material_evaluation(security_id="sec-etf")
    report = _run(
        material_evaluations={"sec-etf": evaluation},
        events_by_id={event.event_key: event},
        universe={"sec-etf": ("EXCLUDED", "ETF"), "sec-chart": ("INCLUDED", "TARGET_MARKET_COMMON_STOCK")},
    )

    origins = {candidate.security_id: candidate.origin for candidate in report.union}
    assert origins["sec-chart"] == "TECHNICAL_ONLY"


def test_the_gate_summary_counts_all_three_decisions():
    events = {}
    evaluations = {}
    for security_id in ("sec-in", "sec-out", "sec-open"):
        event, evaluation = _material_evaluation(security_id=security_id, event_key=f"evt-{security_id}")
        events[event.event_key] = event
        evaluations[security_id] = evaluation

    report = _run(
        material_evaluations=evaluations,
        events_by_id=events,
        universe={
            "sec-in": ("INCLUDED", "TARGET_MARKET_COMMON_STOCK"),
            "sec-out": ("EXCLUDED", "ETF"),
            "sec-open": ("UNRESOLVED", "FOREIGN_STOCK_RULE_PENDING"),
        },
    )

    summary = report.universe_gate.summary
    assert summary["INCLUDED"] == 1
    assert summary["EXCLUDED"] == 1
    assert summary["UNRESOLVED"] == 1
    assert summary["promoted"] == 1
    assert summary["held_research_only"] == 2


# ------------------------------------------------------- the hand-off to Phase 8


def test_every_surviving_answer_becomes_a_setup_row():
    """Including the rejections. A rejected security is a decision that was made,
    and leaving it out would make the setup table a list of hopes."""

    report = _run()

    assert report.setups is not None
    assert len(report.setups.setups) == len(report.stage3.stored)
    assert report.stage_status["setups"] is not StageStatus.SKIPPED_NO_INPUT


def test_no_setup_carries_the_entry_state():
    """analysis.stage3_state has no ENTRY member, so there is nothing to
    translate - and this is the assertion that would notice if one appeared."""

    report = _run()

    assert all(setup.state.value != "ENTRY" for setup in report.setups.setups)


def test_only_the_watch_states_arm_a_watch():
    report = _run()

    for setup in report.setups.setups:
        expected = setup.state.value.startswith("WATCH_")
        assert setup.arms_a_watch is expected


def test_a_setup_from_the_stand_in_says_so():
    report = _run()

    assert report.analysis_is_a_stand_in
    assert any("no entry may be taken from one" in note for note in report.notes)


def test_setups_are_marked_not_live_verified():
    report = _run()

    assert all(
        setup.verification.value == "IMPLEMENTED_NOT_LIVE_VERIFIED"
        for setup in report.setups.setups
    )


def test_a_thesis_key_is_recorded_for_every_setup():
    """Provisional (D-17a is unsettled) and recorded verbatim, so replacing the
    definition later is a versioned change rather than a silent reinterpretation
    of episodes that already exist."""

    report = _run()

    assert all(setup.thesis_key for setup in report.setups.setups)


def test_nothing_reaches_the_setup_stage_when_stage3_did_not_run():
    report = _run(canonical_prompt_sha256="")

    assert report.setups is None
    assert report.stage_status.get("setups") in (None, StageStatus.SKIPPED_NO_INPUT)
