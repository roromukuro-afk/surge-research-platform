"""Phase 7: the input bundle, the stand-in provider, and the output validator.

Most of these test refusals. Stage 3 is the point where a model's output becomes
a record, and the rules about what that record may say - no entry decision, no
prior high as upside, threshold and zone kept apart - only hold if something
checks them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from surge.analysis import (
    SECTION_ORDER,
    BundleError,
    DeterministicMockProvider,
    LLMRequest,
    LLMResponse,
    ProviderKind,
    Stage3Job,
    Stage3State,
    ValidationStatus,
    ZoneBasisKind,
    build_bundle,
    missing_sections,
    render_prompt,
    twenty_percent_threshold,
    validate,
)

CUTOFF = datetime(2026, 9, 17, 6, 30, tzinfo=UTC)
AS_OF = date(2026, 9, 17)
CANONICAL_SHA = "a" * 64


def _bundle(**overrides):
    base = {
        "security_id": "sec-1",
        "market_code": "JP",
        "as_of_date": AS_OF,
        "knowledge_cutoff": CUTOFF,
        "canonical_prompt_sha256": CANONICAL_SHA,
        "stage2": {"close": 1000.0, "atr_pct": 3.0, "concepts_fired": ["VOLUME_DRY_UP"]},
        "routes": {"technical_routes": ["A", "C"], "material_routes": []},
    }
    base.update(overrides)
    return build_bundle(**base)


def _response(**overrides):
    base = {
        "state": Stage3State.WATCH_BREAKOUT,
        "rationale": "volume has contracted into a narrow range above the 20-day average",
        "provider_id": "test",
        "provider_kind": ProviderKind.DETERMINISTIC_MOCK,
    }
    base.update(overrides)
    return LLMResponse(**base)


# ---------------------------------------------------------------------- bundle


def test_the_same_inputs_hash_the_same_and_different_ones_do_not():
    assert _bundle().bundle_sha256 == _bundle().bundle_sha256
    assert _bundle().bundle_sha256 != _bundle(stage2={"close": 1001.0}).bundle_sha256


def test_dictionary_order_does_not_change_the_hash():
    """Determinism is what makes an analysis reproducible at all."""

    first = _bundle(stage2={"close": 1000.0, "atr_pct": 3.0})
    second = _bundle(stage2={"atr_pct": 3.0, "close": 1000.0})
    assert first.bundle_sha256 == second.bundle_sha256


@pytest.mark.parametrize("section", ["canonical_prompt", "addenda"])
def test_the_canonical_prompt_cannot_be_embedded_as_a_section(section):
    """Mixing v5.1 with later decisions is exactly what this prevents."""

    from surge.analysis.bundle import InputBundle

    with pytest.raises(BundleError, match="referenced by hash"):
        InputBundle(
            security_id="sec-1",
            market_code="JP",
            as_of_date=AS_OF,
            knowledge_cutoff=CUTOFF,
            canonical_prompt_sha256=CANONICAL_SHA,
            sections={section: "smuggled"},
        )


def test_addenda_hash_separately_from_the_canonical_prompt():
    bundle = _bundle(addenda_sha256=["b" * 64, "c" * 64])
    digests = bundle.section_digests
    assert digests["canonical_prompt"] == CANONICAL_SHA
    assert digests["addenda"] != CANONICAL_SHA


def test_a_truncated_canonical_hash_is_refused():
    with pytest.raises(BundleError, match="full SHA-256"):
        build_bundle(
            security_id="sec-1",
            market_code="JP",
            as_of_date=AS_OF,
            knowledge_cutoff=CUTOFF,
            canonical_prompt_sha256="abc",
        )


def test_an_unknown_section_is_refused_rather_than_silently_dropped():
    from surge.analysis.bundle import InputBundle

    with pytest.raises(BundleError, match="unknown bundle sections"):
        InputBundle(
            security_id="sec-1",
            market_code="JP",
            as_of_date=AS_OF,
            knowledge_cutoff=CUTOFF,
            canonical_prompt_sha256=CANONICAL_SHA,
            sections={"hunches": []},
        )


def test_an_absent_section_and_an_empty_one_are_different():
    absent = _bundle()
    empty = _bundle(materials=[])
    assert "materials" in missing_sections(absent)
    assert "materials" not in missing_sections(empty)
    assert absent.bundle_sha256 != empty.bundle_sha256


def test_every_section_gets_its_own_digest():
    digests = _bundle(materials=[], coverage={}).section_digests
    assert set(digests) == set(SECTION_ORDER)


def test_the_row_carries_the_versions_that_produced_it():
    row = _bundle(versions={"feature_version": "f1", "stage2_version": "s1"}).as_row(run_id="run-1")
    assert row["feature_version"] == "f1"
    assert row["stage2_version"] == "s1"
    assert row["route_version"] is None
    assert len(row["bundle_sha256"]) == 64


# -------------------------------------------------------------------- provider


def test_the_prompt_keeps_canonical_addenda_and_data_in_separate_blocks():
    prompt = render_prompt(_bundle(), "CANONICAL BODY", ["ADDENDUM ONE"])
    assert "=== CANONICAL v5.1 (immutable) ===" in prompt
    assert "=== POST-v5.1 ADDENDA" in prompt
    assert prompt.index("CANONICAL BODY") < prompt.index("ADDENDUM ONE")
    assert "no price is live" in prompt


def test_the_output_contract_offers_no_entry_state():
    """The states the model is offered stop at setup and watch."""

    contract = render_prompt(_bundle(), "body").split("=== OUTPUT CONTRACT ===")[1]
    offered = contract.split("Return one state from: ")[1].split(".")[0]

    assert "ENTRY" not in offered
    assert set(offered.split(", ")) == {state.value for state in Stage3State}
    assert "Do not return an entry decision" in contract


def test_the_stand_in_is_deterministic():
    provider = DeterministicMockProvider()
    bundle = _bundle()
    first = provider.analyse(LLMRequest(prompt="p", bundle=bundle))
    second = provider.analyse(LLMRequest(prompt="p", bundle=bundle))
    assert first.response_sha256 == second.response_sha256
    assert first.state is second.state


def test_the_stand_in_says_what_it_is():
    """An unlabelled mock verdict in a results table is a trap."""

    response = DeterministicMockProvider().analyse(LLMRequest(prompt="p", bundle=_bundle()))
    assert "deterministic stand-in" in response.rationale
    assert "no evidence at all about the security" in response.confidence_note
    assert response.provider_kind is ProviderKind.DETERMINISTIC_MOCK


def test_no_route_means_reject():
    bundle = _bundle(routes={"technical_routes": [], "material_routes": []})
    response = DeterministicMockProvider().analyse(LLMRequest(prompt="p", bundle=bundle))
    assert response.state is Stage3State.REJECT
    assert response.reachable_zone_high is None


def test_post_close_material_is_a_catalyst_setup_not_a_chart_setup():
    """Post-close news must not be read as though the close had priced it."""

    bundle = _bundle(
        routes={"technical_routes": ["A"], "material_routes": ["M1"]},
        materials=[{"event_id": "e1", "session_timing": "POST_CLOSE"}],
    )
    response = DeterministicMockProvider().analyse(LLMRequest(prompt="p", bundle=bundle))
    assert response.state is Stage3State.POST_CLOSE_CATALYST_SETUP


def test_unknown_session_timing_asserts_neither_setup_state():
    """Three-valued on purpose.

    Whether the close had already priced the disclosure decides between a
    technical setup and a catalyst setup. Without a verified trading calendar
    that is not known, and picking the more convenient of the two claims would
    be the whole failure mode the tri-state exists to prevent.
    """

    bundle = _bundle(
        routes={"technical_routes": ["A"], "material_routes": ["M1"]},
        materials=[{"event_id": "e1", "session_timing": "UNKNOWN"}],
    )
    response = DeterministicMockProvider().analyse(LLMRequest(prompt="p", bundle=bundle))

    assert response.state is Stage3State.WATCH_OTHER
    assert response.state is not Stage3State.TECHNICAL_SETUP_EOD
    assert response.state is not Stage3State.POST_CLOSE_CATALYST_SETUP
    assert "no verified trading calendar" in response.rationale


def test_a_material_with_no_timing_field_at_all_is_treated_as_unknown():
    """A missing field is not a quiet PRE_CLOSE."""

    bundle = _bundle(
        routes={"technical_routes": ["A"], "material_routes": ["M1"]},
        materials=[{"event_id": "e1"}],
    )
    response = DeterministicMockProvider().analyse(LLMRequest(prompt="p", bundle=bundle))
    assert response.state is Stage3State.WATCH_OTHER


def test_pre_close_material_can_still_reach_a_technical_setup():
    bundle = _bundle(
        routes={"technical_routes": ["A"], "material_routes": ["M1"]},
        materials=[{"event_id": "e1", "session_timing": "PRE_CLOSE"}],
    )
    response = DeterministicMockProvider().analyse(LLMRequest(prompt="p", bundle=bundle))
    assert response.state is Stage3State.TECHNICAL_SETUP_EOD


def test_the_zone_is_built_from_volatility_and_resistance_never_from_an_old_high():
    bundle = _bundle(
        stage2={"close": 1000.0, "atr_pct": 3.0, "nearest_resistance": 1040.0, "concepts_fired": []},
        price_obstacles=[{"price_level": 1200.0, "kind": "PRIOR_SURGE_HIGH"}],
    )
    response = DeterministicMockProvider().analyse(LLMRequest(prompt="p", bundle=bundle))

    assert response.reachable_zone_high == pytest.approx(1040.0)
    assert set(response.reachable_zone_basis_kinds) == {
        ZoneBasisKind.VOLATILITY_RANGE,
        ZoneBasisKind.SUPPORT_RESISTANCE,
    }
    assert "treated as resistance rather than as targets" in response.reachable_zone_basis


# ------------------------------------------------------------------- validator


def test_an_entry_instruction_is_rejected():
    result = validate(_response(rationale="Strong setup - buy now at the open."))
    assert result.status is ValidationStatus.REJECTED
    assert any("entry instruction" in error for error in result.errors)


@pytest.mark.parametrize(
    "phrase",
    [
        "there is room back up to its previous high",
        "we target the prior high",
        "upside to the old high of 1,800",
    ],
)
def test_a_prior_high_used_as_upside_is_rejected(phrase):
    result = validate(_response(rationale=phrase))
    assert result.status is ValidationStatus.REJECTED
    assert any("prior high as upside" in error for error in result.errors)


def test_a_prior_high_discussed_as_resistance_is_fine():
    """Discussing old highs as obstacles is wanted, not merely tolerated."""

    result = validate(
        _response(
            rationale="Heavy supply sits at the prior high; any advance has to absorb it before progressing."
        )
    )
    assert result.status is ValidationStatus.PASSED


def test_a_zone_with_no_basis_is_rejected():
    result = validate(_response(reachable_zone_high=1100.0, reachable_zone_low=1000.0))
    assert result.status is ValidationStatus.REJECTED
    assert any("no basis kind" in error for error in result.errors)


def test_a_zone_equal_to_the_threshold_is_rejected():
    """One has been derived from the other, which is the collapse to prevent."""

    result = validate(
        _response(
            reachable_zone_low=1000.0,
            reachable_zone_high=1200.0,
            reachable_zone_basis_kinds=(ZoneBasisKind.VOLATILITY_RANGE,),
            reachable_zone_basis="two ATRs",
        ),
        close=1000.0,
        threshold_price=twenty_percent_threshold(1000.0),
    )
    assert result.status is ValidationStatus.REJECTED
    assert any("same number" in error for error in result.errors)


def test_a_zone_short_of_the_threshold_is_fine():
    result = validate(
        _response(
            reachable_zone_low=1000.0,
            reachable_zone_high=1060.0,
            reachable_zone_basis_kinds=(ZoneBasisKind.VOLATILITY_RANGE,),
            reachable_zone_basis="two ATRs",
        ),
        close=1000.0,
        threshold_price=twenty_percent_threshold(1000.0),
    )
    assert result.status is ValidationStatus.PASSED


def test_a_zone_through_an_unweakened_obstacle_warns_rather_than_fails():
    result = validate(
        _response(
            reachable_zone_low=1000.0,
            reachable_zone_high=1150.0,
            reachable_zone_basis_kinds=(ZoneBasisKind.VOLATILITY_RANGE,),
            reachable_zone_basis="two ATRs",
        ),
        close=1000.0,
        obstacles=[{"price_level": 1100.0}],
    )
    assert result.status is ValidationStatus.PASSED
    assert any("unweakened obstacle" in warning for warning in result.warnings)


def test_a_weakened_obstacle_does_not_warn():
    result = validate(
        _response(
            reachable_zone_low=1000.0,
            reachable_zone_high=1150.0,
            reachable_zone_basis_kinds=(ZoneBasisKind.VOLATILITY_RANGE,),
            reachable_zone_basis="two ATRs",
        ),
        close=1000.0,
        obstacles=[{"price_level": 1100.0, "weakening_evidence": "traded through on 3x volume"}],
    )
    assert result.warnings == []


def test_a_reject_with_a_zone_is_repaired_and_the_repair_is_recorded():
    result = validate(
        _response(
            state=Stage3State.REJECT,
            reachable_zone_low=1000.0,
            reachable_zone_high=1100.0,
            reachable_zone_basis_kinds=(ZoneBasisKind.VOLATILITY_RANGE,),
            reachable_zone_basis="two ATRs",
        )
    )
    assert result.status is ValidationStatus.REPAIRED
    assert any("rejected security has no zone" in repair for repair in result.repairs)


def test_a_missing_section_is_a_warning_not_a_failure():
    result = validate(_response(), missing_sections=("materials",))
    assert result.status is ValidationStatus.PASSED
    assert any("materials" in warning for warning in result.warnings)


def test_an_empty_rationale_is_rejected():
    result = validate(_response(rationale="   "))
    assert result.status is ValidationStatus.REJECTED


def test_the_threshold_is_arithmetic_and_nothing_else():
    assert twenty_percent_threshold(1000.0) == pytest.approx(1200.0)
    assert twenty_percent_threshold(None) is None


# -------------------------------------------------------------------- the job


def _candidate(**overrides):
    base = {
        "security_id": "sec-1",
        "market_code": "JP",
        "stage2": {"close": 1000.0, "atr_pct": 3.0, "nearest_resistance": 1040.0, "concepts_fired": []},
        "routes": {"technical_routes": ["A"], "material_routes": []},
        "threshold_reference_price": 1000.0,
        "threshold_reference_kind": "EOD_CLOSE",
    }
    base.update(overrides)
    return base


def test_the_job_runs_end_to_end_without_any_credential():
    report = Stage3Job().run(
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
        canonical_text="CANONICAL BODY",
        canonical_prompt_sha256=CANONICAL_SHA,
        candidates=[_candidate()],
    )

    assert len(report.results) == 1
    result = report.results[0]
    assert result.state is Stage3State.WATCH_BREAKOUT
    assert result.twenty_percent_threshold_price == pytest.approx(1200.0)
    assert len(result.prompt_sha256) == 64
    assert result.validation.passed


def test_a_provider_that_throws_loses_one_security_not_the_day():
    class Broken:
        provider_id = "broken"
        provider_kind = ProviderKind.HOSTED_LLM
        model_id = None

        def analyse(self, request):
            if request.bundle.security_id == "sec-bad":
                raise TimeoutError("the model did not answer")
            return DeterministicMockProvider().analyse(request)

    report = Stage3Job(provider=Broken()).run(
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
        canonical_text="body",
        canonical_prompt_sha256=CANONICAL_SHA,
        candidates=[_candidate(security_id="sec-bad"), _candidate(security_id="sec-good")],
    )

    assert [r.security_id for r in report.results] == ["sec-good"]
    assert report.skipped[0][0] == "sec-bad"
    assert "TimeoutError" in report.skipped[0][1]


def test_a_rejected_answer_is_reported_but_not_stored_as_an_analysis():
    class Reckless:
        provider_id = "reckless"
        provider_kind = ProviderKind.HOSTED_LLM
        model_id = None

        def analyse(self, request):
            return LLMResponse(
                state=Stage3State.TECHNICAL_SETUP_EOD,
                rationale="Buy now; it should recover to its previous high.",
                provider_id=self.provider_id,
                provider_kind=self.provider_kind,
            )

    report = Stage3Job(provider=Reckless()).run(
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
        canonical_text="body",
        canonical_prompt_sha256=CANONICAL_SHA,
        candidates=[_candidate()],
    )

    assert report.summary["rejected_by_validator"] == 1
    assert report.stored == []
    assert len(report.results[0].validation.errors) == 2


def test_the_report_counts_every_state():
    report = Stage3Job().run(
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
        canonical_text="body",
        canonical_prompt_sha256=CANONICAL_SHA,
        candidates=[
            _candidate(security_id="a"),
            _candidate(security_id="b", routes={"technical_routes": [], "material_routes": []}),
        ],
    )
    assert report.summary["WATCH_BREAKOUT"] == 1
    assert report.summary["REJECT"] == 1
    assert set(report.summary) >= {state.value for state in Stage3State}


def test_the_row_carries_the_hashes_that_make_it_auditable():
    report = Stage3Job().run(
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
        canonical_text="body",
        canonical_prompt_sha256=CANONICAL_SHA,
        candidates=[_candidate()],
    )
    row = report.results[0].as_row(run_id="run-1", bundle_id="bundle-1", available_at=CUTOFF)

    assert len(row["prompt_sha256"]) == 64
    assert len(row["response_sha256"]) == 64
    assert row["provider_kind"] == "DETERMINISTIC_MOCK"
    assert row["state"] in {state.value for state in Stage3State}
    assert "ENTRY" not in row["state"]
