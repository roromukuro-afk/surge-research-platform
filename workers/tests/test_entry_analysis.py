"""The intraday entry contract, and the path from a trigger to a prediction.

Two things are being tested here that nothing else in the suite covers.

The first is that the intraday contract is *different from* Stage 3 in exactly
the ways it should be and no others: ENTRY exists, entry language is allowed,
and the prior-high rule still applies. A contract that merely relaxed Stage 3
would let an end-of-day answer through as an entry.

The second is that the model has no authority. Every one of the entry
preconditions is checked against facts this system measured, and the 3,000 yen
filter in particular is checked again afterwards by code that never saw the
model's answer - so a test that bypasses the validator entirely still cannot get
an over-limit prediction out of the pipeline.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from surge.analysis.entry_analysis import (
    DeterministicIntradayStandIn,
    EntryAnalysisResponse,
    EntryAnalysisState,
    EntryContractError,
    EntryGuardFacts,
    IntradayBundle,
    render_entry_prompt,
    to_entry_request,
    validate_entry_analysis,
)
from surge.analysis.llm import LLMRequest, ProviderKind, Stage3State, ZoneBasisKind
from surge.analysis.validate import ValidationStatus
from surge.entry.decision import decide
from surge.entry.models import (
    AnalysisKind,
    DecisionState,
    EntryAttemptStatus,
    ObservedPrice,
    UniverseVerdict,
    WatchState,
)
from surge.entry.watch import IllegalTransition, Watch
from surge.jobs.entry_analysis_job import WATCH_AFTER, EntryAnalysisJob

CUTOFF = datetime(2026, 9, 17, 2, 15, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 17, 2, 16, tzinfo=UTC)
LATER = datetime(2026, 9, 17, 2, 17, tzinfo=UTC)
CANONICAL = "0" * 64


def _bundle(**sections) -> IntradayBundle:
    base = {
        "security_id": "JP:LOCAL:1234",
        "live_price": {"price": "1000", "observed_at": CUTOFF.isoformat()},
        "stage3_setup": {"state": "WATCH_BREAKOUT"},
        "coverage": {"materials": 1.0},
        "watch": {"trigger_hit": True, "trigger_description": "clears the breakout level"},
    }
    base.update(sections)
    security_id = base.pop("security_id")
    return IntradayBundle(
        security_id=security_id,
        market_code="JP",
        session_date=date(2026, 9, 17),
        decision_cutoff_at=CUTOFF,
        canonical_prompt_sha256=CANONICAL,
        sections=base,
    )


def _price(amount="1000") -> ObservedPrice:
    return ObservedPrice(amount=Decimal(amount), currency="JPY", observed_at=CUTOFF)


def _facts(**overrides) -> EntryGuardFacts:
    base = {
        "universe": UniverseVerdict(decision="INCLUDED"),
        "decision_price": _price(),
        "decision_cutoff_at": CUTOFF,
        "decision_completed_at": COMPLETED,
        "coverage_meets_requirements": True,
        "coverage_detail": "all required collectors reported",
    }
    base.update(overrides)
    return EntryGuardFacts(**base)


def _entry(**overrides) -> EntryAnalysisResponse:
    base = {
        "state": EntryAnalysisState.ENTRY,
        "rationale": "volume expansion through the level with the disclosure unpriced; enter now",
        "provider_id": "groq_hosted",
        "provider_kind": ProviderKind.HOSTED_LLM,
        "model_id": "a-model",
        "decision_price_used": Decimal("1000"),
        "proposed_initial_failure_line": Decimal("940"),
        "reachable_zone_low": Decimal("1010"),
        "reachable_zone_high": Decimal("1150"),
        "reachable_zone_basis_kinds": (ZoneBasisKind.VOLUME_STRUCTURE,),
        "reachable_zone_basis": "the level held on expanding volume for three sessions",
    }
    base.update(overrides)
    return EntryAnalysisResponse(**base)


# ------------------------------------------------------------- the contract


def test_the_intraday_states_are_the_five_and_not_the_setups():
    values = {state.value for state in EntryAnalysisState}

    assert "ENTRY" in values
    assert "TECHNICAL_SETUP_EOD" not in values
    assert "POST_CLOSE_CATALYST_SETUP" not in values
    assert values < {state.value for state in DecisionState}


def test_stage3_still_has_no_entry():
    """The intraday contract adds ENTRY; it does not put one into Stage 3."""

    assert "ENTRY" not in {state.value for state in Stage3State}


def test_entry_language_is_allowed_here():
    """Stage 3 rejects 'enter now' because there is no live price to act on.
    An intraday pass exists to say exactly that."""

    result = validate_entry_analysis(_entry(), _facts())

    assert result.status is ValidationStatus.PASSED
    assert result.errors == []


def test_a_prior_high_as_upside_is_still_refused():
    response = _entry(rationale="thin supply above; it should return to its previous high")

    result = validate_entry_analysis(response, _facts())

    assert any("prior high" in error for error in result.errors)


def test_the_prompt_tells_the_model_it_is_not_the_authority_on_the_limit():
    prompt = render_entry_prompt(_bundle(), "CANONICAL TEXT")

    assert "3000 JPY is out of scope" in prompt
    assert "You are not the authority on this" in prompt
    assert "ENTRY" in prompt


# --------------------------------------------- what an ENTRY has to carry


def test_an_entry_without_a_failure_line_is_refused():
    result = validate_entry_analysis(_entry(proposed_initial_failure_line=None), _facts())

    assert any("initial failure line" in error for error in result.errors)
    assert not result.may_become_a_prediction


def test_a_failure_line_at_or_above_the_price_is_refused():
    result = validate_entry_analysis(
        _entry(proposed_initial_failure_line=Decimal("1000")), _facts()
    )

    assert any("not below the price" in error for error in result.errors)


def test_a_model_that_judged_against_a_different_price_is_refused():
    """The echoed price is how a remembered or invented number is caught."""

    result = validate_entry_analysis(_entry(decision_price_used=Decimal("980")), _facts())

    assert any("judged against 980" in error for error in result.errors)


def test_an_entry_needs_a_reachable_zone_with_a_basis():
    no_zone = validate_entry_analysis(_entry(reachable_zone_high=None), _facts())
    no_basis = validate_entry_analysis(_entry(reachable_zone_basis_kinds=()), _facts())

    assert any("without a reachable zone" in error for error in no_zone.errors)
    assert any("no basis kind" in error for error in no_basis.errors)


def test_the_zone_and_the_twenty_percent_threshold_may_not_be_the_same_number():
    """If they coincide, one was derived from the other and the separation
    between arithmetic and judgement has quietly collapsed."""

    result = validate_entry_analysis(_entry(reachable_zone_high=Decimal("1200")), _facts())

    assert any("+20% threshold are the same number" in error for error in result.errors)


def test_an_entry_outside_the_universe_is_a_system_refusal_not_a_malformed_answer():
    """It has to reach the decision function, because the ledger row is what
    makes the denominator of a hit rate honest."""

    result = validate_entry_analysis(
        _entry(), _facts(universe=UniverseVerdict(decision="UNRESOLVED", reason_code="NO_TYPE"))
    )

    assert any("Only INCLUDED" in refusal for refusal in result.system_refusals)
    assert result.errors == []
    assert result.may_become_a_prediction


def test_an_entry_without_the_required_coverage_is_refused():
    result = validate_entry_analysis(
        _entry(),
        _facts(coverage_meets_requirements=False, coverage_detail="the JP disclosure feed was down"),
    )

    assert any("JP disclosure feed was down" in error for error in result.errors)
    assert not result.may_become_a_prediction


def test_missing_coverage_stops_the_pass_before_the_model_is_called():
    """Not a finding about the security, so nothing is asked and nothing is
    recorded as a decision - and the trigger stays hit for a later pass."""

    watch = _watch()
    provider = _Provider(_entry())
    job = EntryAnalysisJob(provider=provider, canonical_text="CANONICAL")

    decision = job.run_for_watch(
        watch=watch,
        bundle=_bundle(),
        facts=_facts(coverage_meets_requirements=False, coverage_detail="the feed was down"),
        thesis_key="t",
        now=LATER,
    )

    assert provider.prompts == []
    assert not decision.ran_the_model
    assert decision.attempt is None
    assert decision.skipped_reason and "the feed was down" in decision.skipped_reason
    assert watch.state is WatchState.TRIGGER_HIT


def test_a_stand_in_entry_is_refused_by_the_validator_too():
    result = validate_entry_analysis(
        _entry(provider_kind=ProviderKind.DETERMINISTIC_MOCK, provider_id="deterministic_mock"),
        _facts(),
    )

    assert any("deterministic stand-in" in error for error in result.errors)


def test_a_reject_is_not_held_to_the_entry_contract():
    """A REJECT with no failure line is a complete answer."""

    result = validate_entry_analysis(
        EntryAnalysisResponse(
            state=EntryAnalysisState.REJECT,
            rationale="the move is already priced",
            provider_id="groq_hosted",
            provider_kind=ProviderKind.HOSTED_LLM,
        ),
        _facts(),
    )

    assert result.passed


def test_a_missing_bundle_section_is_a_warning_not_an_error():
    bundle = _bundle()
    stripped = replace(bundle, sections={k: v for k, v in bundle.sections.items() if k != "coverage"})

    result = validate_entry_analysis(_entry(), _facts(), bundle=stripped)

    assert result.passed
    assert any("'coverage' was not in the bundle" in warning for warning in result.warnings)


# ------------------------------------------ the limit, and who enforces it


def test_the_validator_reports_an_over_limit_entry_and_still_passes_it_on():
    result = validate_entry_analysis(
        _entry(decision_price_used=Decimal("3500")), _facts(decision_price=_price("3500"))
    )

    assert any("above the 3000 limit" in refusal for refusal in result.system_refusals)
    assert result.errors == []
    # Passed on so the decision function records REJECTED_HARD_FILTER_AT_DECISION.
    assert result.may_become_a_prediction


def test_the_decision_is_what_actually_refuses_an_over_limit_entry():
    """The model has no authority over the hard filter. The validator does not
    have it either: it hands the answer on, and the decision function refuses it
    and writes the row."""

    request = to_entry_request(
        _entry(decision_price_used=Decimal("3500")),
        _facts(decision_price=_price("3500")),
        security_id="JP:LOCAL:1234",
        thesis_key="t",
        bundle=_bundle(),
        entry_price=_price("3500"),
    )
    outcome = decide(request)

    assert outcome.status is EntryAttemptStatus.REJECTED_HARD_FILTER_AT_DECISION
    assert outcome.prediction is None


def test_a_failed_validation_has_no_path_into_the_decision():
    with pytest.raises(EntryContractError, match="did not meet the entry contract"):
        to_entry_request(
            _entry(proposed_initial_failure_line=None),
            _facts(),
            security_id="JP:LOCAL:1234",
            thesis_key="t",
            bundle=_bundle(),
        )


# --------------------------------------------------------------- the job


def _watch(state: WatchState = WatchState.TRIGGER_HIT) -> Watch:
    watch = Watch(
        watch_id="w-1",
        security_id="JP:LOCAL:1234",
        setup_id="s-1",
        trigger_description="clears the breakout level",
        opened_at=CUTOFF,
    )
    if state is WatchState.TRIGGER_HIT:
        watch.trigger(at=CUTOFF, price=Decimal("1000"))
    return watch


class _Provider:
    provider_id = "groq_hosted"
    provider_kind = ProviderKind.HOSTED_LLM
    model_id = "a-model"

    def __init__(self, response):
        self._response = response
        self.prompts: list[str] = []

    def analyse_entry(self, request: LLMRequest):
        self.prompts.append(request.prompt)
        return self._response


def _job(response) -> EntryAnalysisJob:
    return EntryAnalysisJob(provider=_Provider(response), canonical_text="CANONICAL")


def test_the_whole_path_runs_from_a_trigger_to_a_prediction():
    watch = _watch()

    decision = _job(_entry()).run_for_watch(
        watch=watch,
        bundle=_bundle(),
        facts=_facts(),
        thesis_key="WATCH_BREAKOUT|R_A",
        now=LATER,
        entry_price=ObservedPrice(amount=Decimal("1005"), currency="JPY", observed_at=LATER),
        entry_price_method="LAST_TRADE",
    )

    assert decision.created_a_prediction
    assert decision.attempt.status is EntryAttemptStatus.PREDICTION_CREATED
    assert decision.prediction.target_price == Decimal("1206.000000")
    assert decision.watch_state_after is WatchState.ENTERED
    assert [t.to_state for t in watch.history] == [
        WatchState.ARMED,
        WatchState.TRIGGER_HIT,
        WatchState.IN_REANALYSIS,
        WatchState.ENTERED,
    ]


def test_the_reanalysis_is_recorded_before_the_model_is_called():
    """A crash inside the model call must leave a watch that says an analysis
    was under way, not one that says the trigger was never acted on."""

    watch = _watch()
    seen: list[WatchState] = []

    class _Crashes:
        provider_id = "groq_hosted"
        provider_kind = ProviderKind.HOSTED_LLM
        model_id = "a-model"

        def analyse_entry(self, request):
            seen.append(watch.state)
            raise RuntimeError("the provider timed out")

    job = EntryAnalysisJob(provider=_Crashes(), canonical_text="CANONICAL")
    with pytest.raises(RuntimeError):
        job.run_for_watch(
            watch=watch, bundle=_bundle(), facts=_facts(), thesis_key="t", now=LATER
        )

    assert seen == [WatchState.IN_REANALYSIS]
    assert watch.state is WatchState.IN_REANALYSIS


def test_the_job_will_not_run_from_an_armed_watch():
    with pytest.raises(EntryContractError, match="runs from TRIGGER_HIT"):
        _job(_entry()).run_for_watch(
            watch=_watch(WatchState.ARMED),
            bundle=_bundle(),
            facts=_facts(),
            thesis_key="t",
            now=LATER,
        )


def test_a_trigger_can_never_become_an_entry_without_the_reanalysis():
    watch = _watch()

    with pytest.raises(IllegalTransition, match="not a decision to enter"):
        watch.enter(at=LATER)


def test_a_failed_contract_rearms_the_watch_and_writes_no_attempt():
    watch = _watch()

    decision = _job(_entry(proposed_initial_failure_line=None)).run_for_watch(
        watch=watch, bundle=_bundle(), facts=_facts(), thesis_key="t", now=LATER
    )

    assert decision.attempt is None
    assert decision.validation.status is ValidationStatus.REJECTED
    assert decision.watch_state_after is WatchState.REARMED
    assert any("never became a decision" in note for note in decision.notes)


def test_a_reject_still_writes_a_ledger_row():
    """Six of the seven attempt statuses produce no prediction, and that ratio
    is the denominator of any honest hit rate."""

    watch = _watch()
    response = EntryAnalysisResponse(
        state=EntryAnalysisState.REJECT,
        rationale="the disclosure is already in the price",
        provider_id="groq_hosted",
        provider_kind=ProviderKind.HOSTED_LLM,
    )

    decision = _job(response).run_for_watch(
        watch=watch, bundle=_bundle(), facts=_facts(), thesis_key="t", now=LATER
    )

    assert decision.attempt.status is EntryAttemptStatus.REJECTED_BY_ANALYSIS
    assert decision.prediction is None
    assert decision.watch_state_after is WatchState.REARMED


def test_an_entry_price_over_the_limit_rearms_rather_than_closing_the_watch():
    """CLAUDE.md 1-4: if it comes back under the limit that is a new decision,
    so the watch has to still be there to make it."""

    watch = _watch()

    decision = _job(_entry()).run_for_watch(
        watch=watch,
        bundle=_bundle(),
        facts=_facts(),
        thesis_key="t",
        now=LATER,
        entry_price=ObservedPrice(amount=Decimal("3100"), currency="JPY", observed_at=LATER),
    )

    assert decision.attempt.status is EntryAttemptStatus.ENTRY_ABORTED_PRICE_LIMIT
    assert decision.prediction is None
    assert decision.watch_state_after is WatchState.REARMED


def test_an_entry_on_a_security_outside_the_universe_closes_the_watch():
    """The analysis said ENTRY; the universe says no. That is a finding about
    the security, so it reaches the ledger with its own status."""

    watch = _watch()

    decision = _job(_entry()).run_for_watch(
        watch=watch,
        bundle=_bundle(),
        facts=_facts(universe=UniverseVerdict(decision="EXCLUDED", reason_code="NOT_COMMON_STOCK")),
        thesis_key="t",
        now=LATER,
        entry_price=ObservedPrice(amount=Decimal("1005"), currency="JPY", observed_at=LATER),
    )

    assert decision.attempt.status is EntryAttemptStatus.REJECTED_NOT_IN_UNIVERSE
    assert decision.prediction is None
    assert decision.watch_state_after is WatchState.REJECTED
    assert any("Only INCLUDED" in note for note in decision.notes)


def test_an_over_limit_decision_price_still_reaches_the_ledger():
    watch = _watch()

    decision = _job(_entry(decision_price_used=Decimal("3500"))).run_for_watch(
        watch=watch,
        bundle=_bundle(),
        facts=_facts(decision_price=_price("3500")),
        thesis_key="t",
        now=LATER,
    )

    assert decision.attempt.status is EntryAttemptStatus.REJECTED_HARD_FILTER_AT_DECISION
    assert decision.prediction is None
    assert decision.watch_state_after is WatchState.REARMED


def test_the_stand_in_exercises_the_path_and_decides_nothing():
    watch = _watch()
    job = EntryAnalysisJob(provider=DeterministicIntradayStandIn(), canonical_text="CANONICAL")

    decision = job.run_for_watch(
        watch=watch, bundle=_bundle(), facts=_facts(), thesis_key="t", now=LATER
    )

    assert decision.attempt is None
    assert decision.prediction is None
    assert decision.watch_state_after is WatchState.REARMED
    assert any("no evidence about this security" in note for note in decision.notes)


def test_every_attempt_status_says_what_becomes_of_the_watch():
    """A status with no mapping would raise a KeyError in production, at the
    worst possible moment."""

    assert set(WATCH_AFTER) == set(EntryAttemptStatus)


def test_the_bundle_hash_travels_with_the_decision():
    watch = _watch()
    bundle = _bundle()

    decision = _job(_entry()).run_for_watch(
        watch=watch,
        bundle=bundle,
        facts=_facts(),
        thesis_key="t",
        now=LATER,
        entry_price=ObservedPrice(amount=Decimal("1005"), currency="JPY", observed_at=LATER),
    )

    assert decision.bundle_sha256 == bundle.bundle_sha256
    assert decision.prediction.bundle_sha256 == bundle.bundle_sha256
    assert decision.prediction.canonical_prompt_sha256 == CANONICAL


def test_the_bundle_refuses_to_embed_the_canonical_prompt():
    from surge.analysis.bundle import BundleError

    with pytest.raises(BundleError, match="referenced by hash"):
        IntradayBundle(
            security_id="JP:LOCAL:1234",
            market_code="JP",
            session_date=date(2026, 9, 17),
            decision_cutoff_at=CUTOFF,
            canonical_prompt_sha256=CANONICAL,
            sections={"canonical_prompt": "the whole method, inlined"},
        )


def test_the_analysis_kind_must_be_an_intraday_one():
    with pytest.raises(EntryContractError, match="not an intraday analysis"):
        to_entry_request(
            _entry(),
            _facts(),
            security_id="JP:LOCAL:1234",
            thesis_key="t",
            bundle=_bundle(),
            analysis_kind=AnalysisKind.EOD,
        )
