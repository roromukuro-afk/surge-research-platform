"""Phase 10: the labels, and the three ways a miss is not the model's fault.

RF-02 / RF-23 (the three-way miss classification), RF-13 (admission policy and
consistency) and RF-24 (no success label after an invalidated thesis).

The most important tests here are the ones that refuse something: a two-class
target, a judgement that was shown a backfilled document, and a policy that
would train a model to answer for a collector outage.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from surge.labels.admission import (
    FORBIDDEN_TARGETS,
    AdmissionPolicy,
    DatasetPurpose,
    build,
)
from surge.labels.misses import (
    EvidenceDocument,
    assert_no_leakage,
    classify,
    late_but_published,
    visible_at_cutoff,
)
from surge.labels.models import (
    InterpretiveJudgement,
    InterpretiveLabel,
    LabelError,
    ObjectiveLabel,
    ObservationContext,
    ObservationKind,
    ReviewStatus,
)
from surge.labels.objective import for_a_security_never_entered, from_outcome
from surge.outcome.engine import evaluate
from surge.outcome.models import IntradayBar, Session, Trade

CUTOFF = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)
ENTRY_AT = datetime(2026, 9, 17, 2, 6, tzinfo=UTC)
ENTRY = Decimal("1000")
TARGET = Decimal("1200")
FAILURE = Decimal("940")


def _day(index: int) -> date:
    day = date(2026, 9, 17)
    seen = 0
    while True:
        if day.weekday() < 5:
            if seen == index:
                return day
            seen += 1
        day += timedelta(days=1)


def _session(index, *, o=1000, h=1010, low=990, c=1000, **kw) -> Session:
    return Session(
        index=index,
        trade_date=_day(index),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        currency="JPY",
        **kw,
    )


def _entry_session(**kw) -> Session:
    defaults = {
        "intraday": (
            IntradayBar(
                starts_at=ENTRY_AT + timedelta(minutes=1),
                ends_at=ENTRY_AT + timedelta(minutes=2),
                open=Decimal("1000"),
                high=Decimal("1005"),
                low=Decimal("998"),
                close=Decimal("1002"),
            ),
        )
    }
    defaults.update(kw)
    return _session(0, **defaults)


def _outcome(sessions, **kw):
    return evaluate(
        episode_id="ep-1",
        entry_reference_price=ENTRY,
        target_price=TARGET,
        initial_failure_line=FAILURE,
        entry_price_observed_at=ENTRY_AT,
        currency="JPY",
        sessions=sessions,
        **kw,
    )


def _measure(sessions, **kw):
    report = _outcome(sessions, **kw)
    return from_outcome(
        report,
        security_id="sec-1",
        as_of_date=_day(0),
        sessions=sessions,
        entry_at=ENTRY_AT,
    )


# ------------------------------------------------------ objective measurements


def test_a_target_hit_is_measured_as_such():
    label = _measure([_entry_session(), _session(1, h=1250)])

    assert label.hit_20 is True
    assert label.hit_20_before_failure is True
    assert label.failure_before_20 is False
    assert label.days_to_20 == 1


def test_a_failure_first_is_measured_as_such():
    label = _measure([_entry_session(), _session(1, low=900)])

    assert label.failure_before_20 is True
    assert label.hit_20_before_failure is False
    assert label.failure_line_hit is True


def test_an_unresolved_path_leaves_the_order_questions_null_not_false():
    """"We could not tell which came first" and "it did not happen" are
    different facts, and only one of them is evidence."""

    bar = IntradayBar(
        starts_at=datetime(2026, 9, 18, 1, 0, tzinfo=UTC),
        ends_at=datetime(2026, 9, 18, 1, 1, tzinfo=UTC),
        open=Decimal("1000"),
        high=Decimal("1250"),
        low=Decimal("900"),
        close=Decimal("1100"),
    )
    sessions = [
        _entry_session(),
        _session(
            1, o=1000, h=1250, low=900, c=1100, intraday=(bar,), trades_exist_for_this_market=False
        ),
    ]
    label = _measure(sessions)

    assert label.hit_20_before_failure is None
    assert label.failure_before_20 is None
    assert label.failure_line_hit is None
    # The measurement that does not depend on order still stands.
    assert label.hit_20 is True
    assert not label.is_resolved


def test_the_entry_session_is_measured_only_after_the_entry():
    """A high from before the entry would credit the prediction with a price
    that had already gone."""

    before = IntradayBar(
        starts_at=ENTRY_AT - timedelta(minutes=30),
        ends_at=ENTRY_AT - timedelta(minutes=29),
        open=Decimal("1240"),
        high=Decimal("1300"),
        low=Decimal("1230"),
        close=Decimal("1250"),
    )
    sessions = [_session(0, o=1240, h=1300, low=990, c=1000, intraday=(before,)), _session(1)]
    label = _measure(sessions)

    assert label.hit_20 is not True


def test_a_security_nobody_entered_is_measured_without_a_failure_line():
    """These are most of the teacher set. Without them the population is exactly
    what this system already believed."""

    label = for_a_security_never_entered(
        security_id="sec-2",
        as_of_date=_day(0),
        reference_price=ENTRY,
        currency="JPY",
        sessions=[_session(0), _session(1, h=1300)],
        entry_at=ENTRY_AT - timedelta(days=1),
        observation_kind=ObservationKind.ELIGIBLE_ONLY,
    )

    assert label.hit_20 is True
    assert label.episode_id is None
    assert label.failure_line_hit is None
    assert label.hit_20_before_failure is None


# --------------------------------------------- RF-23: three kinds of miss


def _doc(
    doc_id, *, published, available, supports=True, trusted=False
) -> EvidenceDocument:
    return EvidenceDocument(
        document_id=doc_id,
        source_key="a_source",
        source_published_at=published,
        available_to_model_at=available,
        would_have_supported_entry=supports,
        source_timestamp_trusted=trusted,
    )


def test_information_the_system_had_and_dropped_is_the_models_failure():
    docs = [_doc("d1", published=CUTOFF - timedelta(hours=3), available=CUTOFF - timedelta(hours=2))]
    verdict = classify(docs, cutoff=CUTOFF, hit_20=True)

    assert verdict.label is InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE
    assert verdict.is_a_model_failure
    assert verdict.actionable_documents == ("d1",)


def test_information_the_market_had_and_we_did_not_is_the_pipelines_failure():
    """The distinction RF-23 exists for. Counting this as a model miss would
    make the model look worse while hiding a collector outage."""

    docs = [_doc("d1", published=CUTOFF - timedelta(hours=3), available=CUTOFF + timedelta(hours=5))]
    verdict = classify(docs, cutoff=CUTOFF, hit_20=True)

    assert verdict.label is InterpretiveLabel.PIPELINE_MISSED_ACTIONABLE_SIGNAL
    assert not verdict.is_a_model_failure
    assert verdict.late_documents == ("d1",)


def test_the_available_document_wins_over_the_late_one():
    """A is asked first. If the system had something usable, the outage is not
    what caused the miss."""

    docs = [
        _doc("late", published=CUTOFF - timedelta(hours=5), available=CUTOFF + timedelta(hours=1)),
        _doc("had", published=CUTOFF - timedelta(hours=3), available=CUTOFF - timedelta(hours=2)),
    ]
    verdict = classify(docs, cutoff=CUTOFF, hit_20=True)

    assert verdict.label is InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE


def test_nothing_beforehand_is_nobodys_failure():
    docs = [_doc("d1", published=CUTOFF + timedelta(hours=2), available=CUTOFF + timedelta(hours=3))]
    verdict = classify(docs, cutoff=CUTOFF, hit_20=True)

    assert verdict.label is InterpretiveLabel.OUT_OF_SCOPE_SHOCK
    assert not verdict.is_a_model_failure


def test_a_signal_that_existed_but_arrived_too_late_to_act_on():
    docs = [
        _doc(
            "d1",
            published=CUTOFF - timedelta(hours=3),
            available=CUTOFF - timedelta(hours=2),
            supports=False,
        )
    ]
    verdict = classify(docs, cutoff=CUTOFF, hit_20=True, entry_window_had_already_passed=True)

    assert verdict.label is InterpretiveLabel.OUT_OF_SCOPE_LATE


def test_an_untrusted_publication_time_is_flagged_in_the_reason():
    """source_published_at is the publisher's own claim. A misdated document
    would turn a genuine model miss into a pipeline miss."""

    docs = [
        _doc(
            "d1",
            published=CUTOFF - timedelta(hours=3),
            available=CUTOFF + timedelta(hours=5),
            trusted=False,
        )
    ]
    verdict = classify(docs, cutoff=CUTOFF, hit_20=True)

    assert "can be wrong" in verdict.reason


def test_a_trusted_publication_time_does_not_carry_the_caveat():
    docs = [
        _doc(
            "d1",
            published=CUTOFF - timedelta(hours=3),
            available=CUTOFF + timedelta(hours=5),
            trusted=True,
        )
    ]
    verdict = classify(docs, cutoff=CUTOFF, hit_20=True)

    assert "can be wrong" not in verdict.reason


def test_classifying_a_move_that_never_happened_is_refused():
    with pytest.raises(LabelError, match="did reach"):
        classify([], cutoff=CUTOFF, hit_20=False)


def test_the_two_information_sets_do_not_overlap():
    docs = [
        _doc("had", published=CUTOFF - timedelta(hours=3), available=CUTOFF - timedelta(hours=2)),
        _doc("late", published=CUTOFF - timedelta(hours=5), available=CUTOFF + timedelta(hours=1)),
    ]

    available = {d.document_id for d in visible_at_cutoff(docs, CUTOFF)}
    late = {d.document_id for d in late_but_published(docs, CUTOFF)}

    assert available == {"had"}
    assert late == {"late"}
    assert not available & late


def test_showing_a_backfilled_document_to_an_as_of_judgement_is_refused():
    """The whole three-way split collapses if the adjudicator saw the late
    document: it would find the signal obvious and record an outage as a model
    failure."""

    docs = [_doc("late", published=CUTOFF - timedelta(hours=5), available=CUTOFF + timedelta(hours=1))]

    with pytest.raises(LabelError, match="does not become something the system knew"):
        assert_no_leakage(docs, CUTOFF)


def test_a_clean_as_of_set_passes_the_leakage_check():
    docs = [_doc("had", published=CUTOFF - timedelta(hours=3), available=CUTOFF - timedelta(hours=2))]

    assert_no_leakage(docs, CUTOFF)  # returns None; the point is that it does not raise


# ------------------------------------------------------------ admission


def _judgement(label, *, confidence=0.9, review=ReviewStatus.APPROVED, resolved=True, cutoff=CUTOFF):
    objective = ObjectiveLabel(
        security_id="sec-1",
        as_of_date=_day(0),
        observation_kind=ObservationKind.PREDICTED,
        episode_id="ep-1",
        path_resolution="TARGET_FIRST" if resolved else "AMBIGUOUS_PATH",
        primary_episode_outcome="TARGET_HIT" if resolved else "AMBIGUOUS_PATH",
    )
    digest = hashlib.sha256(f"{label}{confidence}{review}".encode()).hexdigest()
    return InterpretiveJudgement(
        objective=objective,
        label=label,
        labeler_model_version="rule-based-labeler-1.0.0",
        information_cutoff_at=cutoff,
        evidence={"why": "fixture"},
        input_sha256=digest,
        confidence=confidence,
        human_review_status=review,
    )


THREE_CLASSES = frozenset(
    {
        InterpretiveLabel.PREDICTIVE_SUCCESS,
        InterpretiveLabel.FALSE_POSITIVE,
        InterpretiveLabel.FAILED_BEFORE_TARGET,
    }
)


def _context(**overrides) -> ObservationContext:
    """A complete input snapshot for the PREDICTED fixture above."""

    base = {
        "objective_id": "obj-1",
        "information_cutoff_at": CUTOFF,
        "production_run_id": "run-prod",
        "universe_run_id": "run-universe",
        "market_data_run_id": "run-market",
        "feature_version": "features-1.0.0",
        "feature_snapshot_ref": "snapshots/features/ep-1",
        "stage3_output_id": "out-1",
        "input_bundle_sha256": "a" * 64,
        "episode_id": "ep-1",
        "entry_attempt_id": "att-1",
        "coverage_snapshot": {"materials": 1.0, "prices": 1.0},
        "verification_status": "LIVE_VERIFIED",
    }
    base.update(overrides)
    return ObservationContext(**base)


def _contexts(judgements, **overrides) -> dict:
    return {j.objective.lineage_key: _context(**overrides) for j in judgements}


def test_a_policy_with_fewer_than_three_classes_is_refused():
    """Two classes here is almost always 'did it rise 20%' under another name."""

    with pytest.raises(LabelError, match="under another name"):
        AdmissionPolicy(
            policy_version="bad-1.0.0",
            description="binary",
            admitted_labels=frozenset(
                {InterpretiveLabel.PREDICTIVE_SUCCESS, InterpretiveLabel.FALSE_POSITIVE}
            ),
        )


@pytest.mark.parametrize(
    "label",
    [
        InterpretiveLabel.PIPELINE_MISSED_ACTIONABLE_SIGNAL,
        InterpretiveLabel.OUT_OF_SCOPE_SHOCK,
        InterpretiveLabel.OUT_OF_SCOPE_LATE,
    ],
)
def test_a_policy_cannot_train_the_model_on_something_it_did_not_do(label):
    with pytest.raises(LabelError, match="could not have done differently"):
        AdmissionPolicy(
            policy_version="bad-2.0.0",
            description="blames the model for the pipeline",
            admitted_labels=THREE_CLASSES | {label},
        )


def test_the_actionable_false_negative_is_admissible():
    """It is the one miss the model could have done something about."""

    policy = AdmissionPolicy(
        policy_version="ok-1.0.0",
        description="fine",
        admitted_labels=THREE_CLASSES | {InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE},
    )

    assert InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE in policy.admitted_labels


@pytest.mark.parametrize("target", sorted(FORBIDDEN_TARGETS))
def test_a_single_measurement_cannot_be_the_training_target(target):
    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )

    with pytest.raises(LabelError, match="predict moves rather than"):
        build(
            [],
            policy=policy,
            name="d",
            knowledge_cutoff=CUTOFF,
            target_field=target,
        )


def test_the_manifest_records_what_it_dropped_and_why():
    policy = AdmissionPolicy(
        policy_version="ok-1.0.0",
        description="fine",
        admitted_labels=THREE_CLASSES,
        min_confidence=0.7,
        required_review_status=frozenset({ReviewStatus.APPROVED}),
    )
    judgements = [
        _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS),
        _judgement(InterpretiveLabel.FALSE_POSITIVE, confidence=0.4),
        _judgement(InterpretiveLabel.FAILED_BEFORE_TARGET, review=ReviewStatus.UNREVIEWED),
        _judgement(InterpretiveLabel.PRICED_IN_ERROR),
        _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS, resolved=False),
    ]

    manifest = build(
        judgements,
        policy=policy,
        name="d",
        knowledge_cutoff=CUTOFF,
        contexts=_contexts(judgements),
    )

    assert len(manifest.admitted) == 1
    assert len(manifest.rejected) == 4
    assert set(manifest.rejection_reasons) == {
        "confidence below 0.7",
        "review status is UNREVIEWED",
        "label is not admitted by this policy",
        "the underlying path was not resolved",
    }


def test_a_judgement_that_saw_more_than_the_dataset_claims_is_rejected():
    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )
    judgement = _judgement(
        InterpretiveLabel.PREDICTIVE_SUCCESS, cutoff=CUTOFF + timedelta(days=1)
    )

    manifest = build(
        [judgement],
        policy=policy,
        knowledge_cutoff=CUTOFF,
        name="d",
        contexts=_contexts([judgement]),
    )

    assert not manifest.admitted
    assert "information cutoff is after" in manifest.rejected[0].reason


def test_a_dataset_that_collapsed_to_two_classes_says_so():
    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )
    judgements = [
        _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS),
        _judgement(InterpretiveLabel.FALSE_POSITIVE),
    ]

    manifest = build(
        judgements,
        policy=policy,
        name="d",
        knowledge_cutoff=CUTOFF,
        contexts=_contexts(judgements),
    )

    assert len(manifest.admitted) == 2
    assert any("not usable as a training target" in note for note in manifest.notes)


def test_only_the_actionable_miss_counts_as_a_model_failure():
    assert _judgement(InterpretiveLabel.ACTIONABLE_FALSE_NEGATIVE).is_a_model_failure
    assert not _judgement(InterpretiveLabel.PIPELINE_MISSED_ACTIONABLE_SIGNAL).is_a_model_failure
    assert not _judgement(InterpretiveLabel.OUT_OF_SCOPE_SHOCK).is_a_model_failure
    assert not _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS).is_a_model_failure


def test_an_entry_session_trade_counts_toward_the_measurement():
    sessions = [
        _session(
            0,
            o=990,
            h=1300,
            low=980,
            c=1250,
            trades=(Trade(at=ENTRY_AT + timedelta(minutes=5), price=Decimal("1210")),),
        )
    ]
    label = _measure(sessions)

    assert label.hit_20 is True
    assert label.days_to_20 == 0


# --------------------------------------- identity, lineage and the default policy


def test_two_setups_on_one_security_on_one_day_are_two_examples():
    """The old rule said one teacher example per security per day. A security
    can have two setups, two theses and two episodes on the same day, and they
    have different inputs and different answers."""

    first = ObjectiveLabel(
        security_id="sec-1",
        as_of_date=_day(0),
        observation_kind=ObservationKind.SETUP_NOT_ENTERED,
        setup_id="setup-a",
    )
    second = ObjectiveLabel(
        security_id="sec-1",
        as_of_date=_day(0),
        observation_kind=ObservationKind.SETUP_NOT_ENTERED,
        setup_id="setup-b",
    )

    assert first.identity != second.identity


def test_each_observation_kind_is_identified_by_its_own_thing():
    predicted = ObjectiveLabel(
        security_id="sec-1",
        as_of_date=_day(0),
        observation_kind=ObservationKind.PREDICTED,
        episode_id="ep-1",
    )
    eligible = ObjectiveLabel(
        security_id="sec-1",
        as_of_date=_day(0),
        observation_kind=ObservationKind.ELIGIBLE_ONLY,
    )

    assert predicted.identity[0] == "PREDICTED"
    assert predicted.identity[1] == "ep-1"
    assert eligible.identity[0] == "ELIGIBLE_ONLY"
    # Same security and day, and still not the same example.
    assert predicted.identity != eligible.identity


def test_a_context_without_a_bundle_or_a_run_is_not_reproducible():
    """It records that a decision happened, not what it was made from."""

    from surge.labels.models import ObservationContext

    bare = ObservationContext(objective_id="obj-1", information_cutoff_at=CUTOFF)
    full = ObservationContext(
        objective_id="obj-1",
        information_cutoff_at=CUTOFF,
        production_run_id="run-1",
        feature_version="features-1.0.0",
        input_bundle_sha256="a" * 64,
    )

    assert not bare.is_reproducible
    assert full.is_reproducible


def test_the_default_policy_cannot_admit_an_unexplained_rise():
    """PRICE_SUCCESS_EXOGENOUS means the price rose and the thesis does not
    explain why. Training a predictive model on it teaches it to claim credit
    for luck."""

    with pytest.raises(LabelError, match="take credit for luck"):
        AdmissionPolicy(
            policy_version="bad-3.0.0",
            description="admits luck",
            admitted_labels=THREE_CLASSES | {InterpretiveLabel.PRICE_SUCCESS_EXOGENOUS},
        )


def test_a_special_purpose_policy_may_admit_it_by_saying_why():
    """The label stays useful for research. It just has to be asked for."""

    policy = AdmissionPolicy(
        policy_version="error-analysis-1.0.0",
        description="for error analysis",
        admitted_labels=THREE_CLASSES | {InterpretiveLabel.PRICE_SUCCESS_EXOGENOUS},
        special_purpose_reason="error analysis of rises the thesis did not anticipate",
    )

    assert policy.is_special_purpose
    assert InterpretiveLabel.PRICE_SUCCESS_EXOGENOUS in policy.admitted_labels


# --------------------------------------------------- teacher input lineage


def test_a_training_set_cannot_be_built_without_the_input_side():
    """Teacher data is input snapshot + decision + outcome. A set built from the
    last two teaches a model from decisions whose inputs were never recorded."""

    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )

    with pytest.raises(LabelError, match="input snapshot"):
        build([], policy=policy, name="d", knowledge_cutoff=CUTOFF)


def test_a_label_with_no_context_is_kept_and_not_admitted():
    """The outcome happened. Dropping the label would bias the record of what
    happened; admitting it would train a model on an input nobody wrote down."""

    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )
    judgement = _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS)

    manifest = build([judgement], policy=policy, name="d", knowledge_cutoff=CUTOFF, contexts={})

    assert not manifest.admitted
    assert "no observation context" in manifest.rejected[0].reason


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        ({"coverage_snapshot": None}, "coverage_snapshot"),
        ({"feature_version": None, "feature_snapshot_ref": None}, "no feature_version"),
        ({"feature_snapshot_ref": None}, "no snapshot or reference"),
        ({"production_run_id": None}, "no production_run_id"),
        ({"universe_run_id": None}, "no universe_run_id"),
        ({"market_data_run_id": None}, "no market_data_run_id"),
        ({"episode_id": None}, "PREDICTED with no episode_id"),
        ({"entry_attempt_id": None}, "PREDICTED with no entry_attempt_id"),
        ({"stage3_output_id": None, "input_bundle_sha256": None}, "no decision reference"),
    ],
)
def test_each_missing_piece_of_lineage_blocks_admission(gap, expected):
    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )
    judgement = _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS)

    manifest = build(
        [judgement],
        policy=policy,
        name="d",
        knowledge_cutoff=CUTOFF,
        contexts=_contexts([judgement], **gap),
    )

    assert not manifest.admitted
    assert expected in manifest.rejected[0].reason


def test_an_absent_feature_version_may_be_explained_rather_than_guessed():
    """No features and nobody-wrote-down-which-features are different examples,
    and only one of them is usable."""

    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )
    judgement = _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS)

    manifest = build(
        [judgement],
        policy=policy,
        name="d",
        knowledge_cutoff=CUTOFF,
        contexts=_contexts(
            [judgement],
            feature_version=None,
            feature_snapshot_ref=None,
            no_feature_reason="recorded before the feature engine ran for this market",
        ),
    )

    assert len(manifest.admitted) == 1


def test_an_eligible_only_observation_is_not_asked_for_an_episode():
    """Demanding one would reject the entire population of securities nobody
    surfaced - which is the part of the teacher set that teaches about misses."""

    context = _context(
        episode_id=None, entry_attempt_id=None, stage3_output_id=None, input_bundle_sha256=None
    )

    assert context.lineage_gaps(ObservationKind.ELIGIBLE_ONLY) == ()
    assert context.lineage_gaps(ObservationKind.PREDICTED)


def test_a_setup_observation_needs_its_setup():
    complete = _context(setup_id="setup-1")
    without = _context(setup_id=None)

    assert complete.lineage_gaps(ObservationKind.SETUP_NOT_ENTERED) == ()
    assert "no setup_id" in " ".join(without.lineage_gaps(ObservationKind.SETUP_NOT_ENTERED))


def test_a_context_that_saw_more_than_its_judgement_is_refused():
    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )
    judgement = _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS)

    manifest = build(
        [judgement],
        policy=policy,
        name="d",
        knowledge_cutoff=CUTOFF,
        contexts=_contexts([judgement], information_cutoff_at=CUTOFF + timedelta(hours=1)),
    )

    assert not manifest.admitted
    assert "could not have known" in manifest.rejected[0].reason


def test_a_research_set_may_skip_lineage_and_has_to_say_so():
    policy = AdmissionPolicy(
        policy_version="ok-1.0.0", description="fine", admitted_labels=THREE_CLASSES
    )
    judgement = _judgement(InterpretiveLabel.PREDICTIVE_SUCCESS)

    manifest = build(
        [judgement],
        policy=policy,
        name="d",
        knowledge_cutoff=CUTOFF,
        purpose=DatasetPurpose.RESEARCH_ONLY,
    )

    assert len(manifest.admitted) == 1
    assert manifest.summary["purpose"] == "RESEARCH_ONLY"
    assert any("must not be used to train a production model" in n for n in manifest.notes)
