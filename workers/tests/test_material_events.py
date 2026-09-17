"""Phase 5: merging, relevance, entity relations and Material Routes M1-M6."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from surge.material import (
    DIRECT_RELATIONS,
    MACRO_RELATIONS,
    MATERIAL_ROUTE_VERSION,
    ROUTE_ACCEPTS,
    DocumentFacts,
    EntityRelation,
    EventSecurityFeatures,
    EventSource,
    LinkConfidence,
    MarketRelevance,
    MaterialError,
    MaterialEvent,
    RelationType,
    SourceRole,
    build_candidate,
    build_signals,
    decide,
    evaluate_routes,
    merge_documents,
    strongest,
)
from surge.material.routes import direct_routes_stay_direct, macro_routes_require_a_mechanism
from surge.news.models import TimePrecision

T0 = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


def source(key="tdnet", role=SourceRole.DISCOVERY, at=T0, document_id="doc-1"):
    return EventSource(document_id=document_id, source_key=key, role=role, available_to_model_at=at)


def event(sources=None, event_type="EARNINGS_REVISION", key="evt-1"):
    return MaterialEvent(
        event_key=key,
        event_type=event_type,
        scope="JP",
        merge_version="event-merge-1.0.0",
        sources=tuple(sources or [source()]),
        event_id=key,
    )


def features(**overrides):
    base = {
        "event_id": "evt-1",
        "security_id": "sec-1",
        "feature_version": "material-feature-1.0.0",
        "knowledge_cutoff": CUTOFF,
        "methods": {
            name: f"{name}-method" for name in EventSecurityFeatures.FEATURE_NAMES
        },
    }
    base.update(overrides)
    return EventSecurityFeatures(**base)


def relation(relation_type=RelationType.DIRECT_COMPANY, causal_path=None, confidence=LinkConfidence.STRONG):
    return EntityRelation(
        relation_type=relation_type,
        confidence=confidence,
        extractor="test",
        extractor_version="1.0.0",
        security_id="sec-1",
        causal_path=causal_path,
    )


# --------------------------------------------------------------------------- models


def test_an_event_needs_someone_to_have_found_it():
    with pytest.raises(MaterialError, match="no DISCOVERY source"):
        MaterialEvent(
            event_key="k",
            event_type="T",
            scope="JP",
            merge_version="event-merge-1.0.0",
            sources=(source(role=SourceRole.VERIFICATION),),
        )


def test_twenty_reprints_of_one_wire_story_are_one_source():
    """Corroboration is not confirmation, and the count has to say so."""

    reprints = [source(key="wire", document_id="doc-0")] + [
        source(key=f"outlet_{n}", role=SourceRole.CORROBORATION, document_id=f"doc-{n}") for n in range(1, 21)
    ]
    assert event(reprints).independent_source_count == 1
    assert event(reprints).is_independently_verified is False


def test_two_publishers_in_discovery_and_verification_roles_count_as_two():
    pair = [source(key="tdnet"), source(key="nikkei", role=SourceRole.VERIFICATION, document_id="doc-2")]
    assert event(pair).independent_source_count == 2
    assert event(pair).is_independently_verified is True


def test_first_known_at_is_the_earliest_source_not_the_latest():
    pair = [
        source(key="a", at=T0 + timedelta(hours=3)),
        source(key="b", role=SourceRole.VERIFICATION, at=T0, document_id="doc-2"),
    ]
    assert event(pair).first_known_at == T0


def test_macro_relation_without_a_mechanism_is_refused():
    for relation_type in sorted(MACRO_RELATIONS):
        with pytest.raises(MaterialError, match="causal_path"):
            relation(relation_type)


def test_macro_relation_with_a_mechanism_is_accepted():
    built = relation(RelationType.FX_EXPOSURE, causal_path="70% of revenue is USD-denominated")
    assert built.causal_path.startswith("70%")


def test_a_relation_must_name_something():
    with pytest.raises(MaterialError, match="security or an issuer"):
        EntityRelation(
            relation_type=RelationType.DIRECT_COMPANY,
            confidence=LinkConfidence.STRONG,
            extractor="test",
            extractor_version="1.0.0",
        )


def test_strongest_prefers_the_shortest_inference_chain():
    assert strongest([RelationType.INDUSTRY, RelationType.DIRECT_COMPANY]) is RelationType.DIRECT_COMPANY
    assert strongest([RelationType.WEAK_ASSOCIATION, RelationType.SUPPLIER]) is RelationType.SUPPLIER
    assert strongest([]) is None


def test_a_feature_value_needs_a_reproducible_method():
    with pytest.raises(MaterialError, match="no method"):
        EventSecurityFeatures(
            event_id="e",
            security_id="s",
            feature_version="v",
            knowledge_cutoff=CUTOFF,
            novelty=0.9,
        )


def test_features_outside_the_unit_range_are_refused():
    with pytest.raises(MaterialError, match=r"outside \[0, 1\]"):
        features(novelty=1.4)


def test_unmeasured_features_are_named_rather_than_zeroed():
    computed = features(novelty=0.8, surprise=0.5)
    assert computed.measured == {"novelty": 0.8, "surprise": 0.5}
    assert set(computed.unmeasured) == {
        "directness",
        "magnitude",
        "persistence",
        "market_reaction",
        "priced_in",
    }


# --------------------------------------------------------------------------- merging


def facts(document_id, source_key, *, published=None, precision=TimePrecision.EXACT, title="Earnings revised up",
          entity="JP:1234", references=()):
    return DocumentFacts(
        document_id=document_id,
        source_key=source_key,
        scope="JP",
        event_type="EARNINGS_REVISION",
        entity_key=entity,
        available_to_model_at=published or T0,
        published_at=published or T0,
        published_precision=precision,
        title=title,
        references=references,
    )


def test_same_entity_and_precise_minute_merges_across_outlets():
    events, _ = merge_documents(
        [
            facts("d1", "tdnet", published=T0),
            facts("d2", "nikkei", published=T0 + timedelta(seconds=40), title="Company raises guidance"),
        ]
    )
    assert len(events) == 1
    assert events[0].independent_source_count == 2
    roles = {s.source_key: s.role for s in events[0].sources}
    assert roles["tdnet"] is SourceRole.DISCOVERY
    assert roles["nikkei"] is SourceRole.VERIFICATION


def test_a_publishers_own_follow_up_only_corroborates():
    events, _ = merge_documents(
        [
            facts("d1", "tdnet", published=T0),
            facts("d2", "tdnet", published=T0 + timedelta(seconds=30)),
        ]
    )
    assert len(events) == 1
    assert events[0].independent_source_count == 1
    assert [s.role for s in events[0].sources] == [SourceRole.DISCOVERY, SourceRole.CORROBORATION]


def test_without_a_precise_time_we_split_rather_than_guess():
    """False split over false merge, as the identity model already decided."""

    events, candidates = merge_documents(
        [
            facts("d1", "tdnet", precision=TimePrecision.DATE_ONLY),
            facts("d2", "nikkei", precision=TimePrecision.DATE_ONLY),
        ]
    )
    assert len(events) == 2
    # ...but the split is visible rather than silent.
    assert len(candidates) == 1
    assert candidates[0]["entity_key"] == "JP:1234"
    assert candidates[0]["title_similarity"] == 1.0


def test_an_explicit_cross_reference_joins_what_the_key_split():
    events, _ = merge_documents(
        [
            facts("d1", "tdnet", precision=TimePrecision.DATE_ONLY),
            facts("d2", "nikkei", precision=TimePrecision.DATE_ONLY, references=("d1",)),
        ]
    )
    assert len(events) == 1


def test_different_entities_never_merge_however_alike_the_headlines():
    events, candidates = merge_documents(
        [
            facts("d1", "tdnet", entity="JP:1234"),
            facts("d2", "tdnet", entity="JP:5678"),
        ]
    )
    assert len(events) == 2
    assert candidates == []


def test_an_hour_apart_is_two_events_not_one():
    events, _ = merge_documents(
        [
            facts("d1", "tdnet", published=T0),
            facts("d2", "nikkei", published=T0 + timedelta(hours=1)),
        ]
    )
    assert len(events) == 2


# ------------------------------------------------------------------------- relevance


def signals_for(relations, **overrides):
    return build_signals(event(), relations, **overrides)


def test_an_event_with_nothing_to_attach_it_to_is_not_relevant():
    decision = decide(event(), signals_for([]))
    assert decision.relevance is MarketRelevance.NOT_RELEVANT
    assert decision.reason_code == "NO_RESOLVABLE_ENTITY"
    assert "retained" in decision.reason_detail


def test_a_regulated_disclosure_about_a_known_issuer_is_relevant():
    decision = decide(
        event(),
        signals_for([relation()], document_types=("TIMELY_DISCLOSURE",)),
    )
    assert decision.relevance is MarketRelevance.RELEVANT
    assert decision.reason_code == "REGULATED_DISCLOSURE_ABOUT_KNOWN_ISSUER"


def test_macro_without_a_mechanism_is_uncertain_not_discarded():
    """The mechanism may exist; the extractor may simply have missed it."""

    macro = relation(RelationType.POLICY_EXPOSURE, causal_path="tariff raises input cost")
    weakened = build_signals(event(), [macro])
    stripped = type(weakened)(
        resolved_entities=weakened.resolved_entities,
        strongest_relation=weakened.strongest_relation,
        relations_with_causal_path=0,
        from_regulated_disclosure=False,
        has_quantified_magnitude=False,
        independent_source_count=weakened.independent_source_count,
        document_count=weakened.document_count,
    )
    decision = decide(event(), stripped)
    assert decision.relevance is MarketRelevance.UNCERTAIN
    assert decision.reason_code == "MACRO_WITHOUT_MECHANISM"
    assert decision.blocks_candidacy is True


def test_macro_with_a_mechanism_is_relevant():
    macro = relation(RelationType.RATE_EXPOSURE, causal_path="floating-rate debt is 60% of the balance sheet")
    decision = decide(event(), signals_for([macro]))
    assert decision.relevance is MarketRelevance.RELEVANT
    assert decision.reason_code == "MACRO_WITH_STATED_MECHANISM"


def test_weak_association_alone_is_uncertain():
    decision = decide(event(), signals_for([relation(RelationType.WEAK_ASSOCIATION)]))
    assert decision.relevance is MarketRelevance.UNCERTAIN
    assert decision.reason_code == "WEAK_ASSOCIATION_ONLY"


def test_every_decision_carries_the_signals_it_rested_on():
    decision = decide(event(), signals_for([relation()]))
    payload = decision.signals.as_dict()
    assert payload["resolved_entities"] == 1
    assert payload["strongest_relation"] == "DIRECT_COMPANY"
    assert "independent_source_count" in payload


# ---------------------------------------------------------------------------- routes


def test_weak_association_alone_fires_nothing():
    fired, evidence, refusals = evaluate_routes(
        event(),
        [relation(RelationType.WEAK_ASSOCIATION)],
        features(novelty=1.0, surprise=1.0, directness=1.0, magnitude=1.0, priced_in=0.0),
    )
    assert fired == []
    assert evidence == {}
    assert "WEAK_ASSOCIATION" in refusals["ALL"]


def test_no_route_definition_accepts_weak_association():
    for code, accepted in ROUTE_ACCEPTS.items():
        assert RelationType.WEAK_ASSOCIATION not in accepted, code


def test_m1_fires_on_a_regulated_disclosure_about_the_issuer():
    fired, evidence, _ = evaluate_routes(
        event(),
        [relation()],
        features(novelty=0.8),
        document_types=("TIMELY_DISCLOSURE",),
    )
    assert "M1" in fired
    assert evidence["M1"]["novelty"] == 0.8


def test_m1_does_not_fire_on_a_press_release():
    fired, _, refusals = evaluate_routes(
        event(), [relation()], features(novelty=0.9), document_types=("PRESS_RELEASE",)
    )
    assert "M1" not in fired
    assert "regulated_documents=0" in refusals["M1"]


def test_m2_needs_a_second_independent_publisher():
    single = event([source(key="tdnet")])
    fired, _, _ = evaluate_routes(single, [relation()], features(directness=0.9))
    assert "M2" not in fired

    pair = event([source(key="tdnet"), source(key="nikkei", role=SourceRole.VERIFICATION, document_id="d2")])
    fired, evidence, _ = evaluate_routes(pair, [relation()], features(directness=0.9))
    assert "M2" in fired
    assert evidence["M2"]["independent_sources"] == 2


def test_macro_routes_refuse_an_exposure_with_no_written_path():
    """The engine refuses it even though the dataclass would too.

    Belt and braces on purpose: a relation loaded from the database bypasses the
    dataclass validation, and a macro claim with no mechanism must not fire a
    route by that route.
    """

    unpathed = EntityRelation.__new__(EntityRelation)
    object.__setattr__(unpathed, "relation_type", RelationType.COMMODITY_EXPOSURE)
    object.__setattr__(unpathed, "confidence", LinkConfidence.PROVISIONAL)
    object.__setattr__(unpathed, "extractor", "test")
    object.__setattr__(unpathed, "extractor_version", "1.0.0")
    object.__setattr__(unpathed, "security_id", "sec-1")
    object.__setattr__(unpathed, "issuer_id", None)
    object.__setattr__(unpathed, "causal_path", None)
    object.__setattr__(unpathed, "evidence", None)

    fired, _, refusals = evaluate_routes(event(), [unpathed], features(magnitude=0.9))
    assert "M5" not in fired
    assert "causal_path=False" in refusals["M5"]


def test_m5_fires_with_a_written_path_and_enough_magnitude():
    fx = relation(RelationType.FX_EXPOSURE, causal_path="70% of revenue is USD-denominated")
    fired, evidence, _ = evaluate_routes(event(), [fx], features(magnitude=0.7))
    assert "M5" in fired
    assert evidence["M5"]["causal_path"].startswith("70%")


def test_m6_needs_priced_in_to_have_been_measured():
    """An unmeasurable priced-in is not evidence that nothing is priced in."""

    fired, _, refusals = evaluate_routes(event(), [relation()], features(surprise=0.9))
    assert "M6" not in fired
    assert "priced_in=None" in refusals["M6"]

    fired, _, _ = evaluate_routes(event(), [relation()], features(surprise=0.9, priced_in=0.1))
    assert "M6" in fired


def test_a_missing_feature_never_clears_a_threshold():
    fired, _, _ = evaluate_routes(event(), [relation()], features(), document_types=("TIMELY_DISCLOSURE",))
    assert fired == []


def test_routes_are_or_type_so_one_is_enough():
    fired, _, _ = evaluate_routes(
        event(), [relation()], features(novelty=0.9), document_types=("STATUTORY_FILING",)
    )
    assert fired == ["M1"]


def test_a_security_reached_twice_keeps_both_routes():
    fx = relation(RelationType.FX_EXPOSURE, causal_path="70% of revenue is USD-denominated")
    candidate = build_candidate(
        "sec-1",
        [
            (event(key="evt-1"), [relation()], features(novelty=0.9), ("TIMELY_DISCLOSURE",)),
            (event(key="evt-2"), [fx], features(event_id="evt-2", magnitude=0.8), ()),
        ],
    )
    assert candidate.discovery_routes == ["M1", "M5"]
    assert candidate.route_count == 2
    assert set(candidate.event_ids) == {"evt-1", "evt-2"}
    assert candidate.strongest_relation is RelationType.DIRECT_COMPANY


def test_a_security_that_fires_nothing_is_not_a_candidate_but_keeps_its_refusals():
    candidate = build_candidate("sec-1", [(event(), [relation()], features(), ())])
    assert candidate.is_candidate is False
    assert candidate.discovery_routes == []
    assert "evt-1" in candidate.refusals


def test_route_definitions_match_their_stated_shape():
    assert macro_routes_require_a_mechanism()
    assert direct_routes_stay_direct()
    assert ROUTE_ACCEPTS["M1"] <= DIRECT_RELATIONS
    assert MATERIAL_ROUTE_VERSION == "material-route-1.0.0"
