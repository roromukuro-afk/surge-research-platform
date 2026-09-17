"""Phase 11: the frame, and the fact that it refuses.

RF-15 in spirit (research stays research) and CLAUDE.md 1-15/1-16/1-17: no random
shuffle split, no promotion without walk-forward, no probability without a
calibration.

The test that matters most is the last one. Today the gate refuses everything,
and it should keep refusing until there is real teacher data - so the refusal is
asserted rather than left as a comment that stops being true silently.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from surge.modellab.champion import Comparison, ComparisonError, FoldScore
from surge.modellab.promotion import (
    MINIMUM_CLASSES,
    MINIMUM_FOLDS,
    MINIMUM_LABELS,
    Evidence,
    describe_current_state,
)
from surge.modellab.promotion import evaluate as gate
from surge.modellab.walkforward import (
    Fold,
    LeakageError,
    assert_no_leakage,
    build_folds,
    split,
)

START = date(2024, 1, 1)
END = date(2026, 1, 1)


# ------------------------------------------------------------- walk-forward


def test_folds_are_time_ordered_and_never_overlap():
    folds = build_folds(start=START, end=END, train_days=180, test_days=60)

    assert folds
    for fold in folds:
        assert fold.train_end < fold.purge_start
        assert fold.purge_end < fold.test_start
        assert fold.test_start > fold.train_end


def test_the_purge_gap_is_at_least_the_horizon():
    """A label dated at the end of training is not settled until about S20. A
    shorter gap trains on answers that came from inside the test window."""

    folds = build_folds(start=START, end=END, train_days=180, test_days=60)

    assert all(fold.purge_days >= 30 for fold in folds)


def test_a_shorter_purge_gap_is_refused():
    with pytest.raises(LeakageError, match="shorter than"):
        build_folds(start=START, end=END, train_days=180, test_days=60, purge_days=5)


def test_a_longer_purge_gap_is_allowed():
    folds = build_folds(start=START, end=END, train_days=180, test_days=60, purge_days=60)

    assert all(fold.purge_days == 60 for fold in folds)


def test_a_fold_whose_test_window_starts_inside_the_purge_is_refused():
    with pytest.raises(LeakageError, match="inside the purge gap"):
        Fold(
            index=0,
            train_start=START,
            train_end=START + timedelta(days=100),
            purge_start=START + timedelta(days=101),
            purge_end=START + timedelta(days=140),
            test_start=START + timedelta(days=120),
            test_end=START + timedelta(days=180),
        )


def test_rows_inside_the_purge_gap_go_into_neither_side():
    """The gap is a deliberate hole, not a third dataset."""

    folds = build_folds(start=START, end=END, train_days=180, test_days=60)
    fold = folds[0]
    rows = [
        (fold.train_end, "train"),
        (fold.purge_start, "purged"),
        (fold.purge_end, "purged"),
        (fold.test_start, "test"),
    ]

    train, test = split(fold, rows)

    assert train == ["train"]
    assert test == ["test"]


def test_there_is_no_shuffle_option():
    """CLAUDE.md 1-16 forbids a random split. It is absent, not discouraged."""

    import inspect

    from surge.modellab import walkforward

    source = inspect.getsource(walkforward)
    assert "shuffle" not in source.lower().replace("no shuffle", "").replace(
        "random shuffle split", ""
    )
    assert "random" not in inspect.signature(build_folds).parameters


def test_a_training_label_settled_inside_the_test_window_is_caught():
    """The leak the window boundaries cannot see.

    The purge gap is a calendar approximation of twenty trading sessions. A run
    of holidays, a suspension, or an episode that stayed open past S20 stretches
    the real settlement past the gap - and then a training label's answer came
    from inside the test window after all.
    """

    fold = build_folds(start=START, end=END, train_days=180, test_days=60)[0]
    late = (fold.train_end, fold.test_start + timedelta(days=1))

    with pytest.raises(LeakageError, match="not settled until inside"):
        assert_no_leakage(fold, [late])


def test_labels_settled_before_the_test_window_are_fine():
    fold = build_folds(start=START, end=END, train_days=180, test_days=60)[0]
    in_time = (fold.train_end, fold.purge_end)

    assert_no_leakage(fold, [in_time])  # returns None; the point is that it does not raise


# --------------------------------------------------------- champion vs challenger


def _scores(indices, value):
    return [FoldScore(fold_index=i, score=value, labels_in_test=100) for i in indices]


def test_a_comparison_on_different_folds_is_refused():
    """A model scored on an easier period is not a better model."""

    with pytest.raises(ComparisonError, match="different folds"):
        Comparison(
            champion_version="v1",
            challenger_version="v2",
            champion_scores=_scores([0, 1], 0.5),
            challenger_scores=_scores([0, 1, 2], 0.6),
        )


def test_a_strictly_better_challenger_wins():
    comparison = Comparison(
        champion_version="v1",
        challenger_version="v2",
        champion_scores=_scores([0, 1, 2], 0.50),
        challenger_scores=_scores([0, 1, 2], 0.55),
    )

    assert comparison.challenger_wins is True


def test_a_tie_is_not_a_win():
    """Promoting on a tie makes the champion drift with every run."""

    comparison = Comparison(
        champion_version="v1",
        challenger_version="v2",
        champion_scores=_scores([0, 1, 2], 0.50),
        challenger_scores=_scores([0, 1, 2], 0.50),
    )

    assert comparison.challenger_wins is False


def test_an_unopposed_challenger_has_not_beaten_anything():
    """Otherwise the first model becomes a champion by default."""

    comparison = Comparison(
        champion_version=None,
        challenger_version="v1",
        challenger_scores=_scores([0, 1, 2], 0.6),
    )

    assert comparison.challenger_wins is None


# ------------------------------------------------------------- the gate


def _good_evidence(**overrides) -> Evidence:
    base = {
        "model_version": "v2",
        "dataset_name": "d",
        "dataset_policy_version": "admission-1.0.0",
        "admitted_labels": MINIMUM_LABELS,
        "class_counts": {"PREDICTIVE_SUCCESS": 80, "FALSE_POSITIVE": 80, "PRICED_IN_ERROR": 40},
        "folds_evaluated": MINIMUM_FOLDS,
        "labels_are_live_verified": True,
        "calibration_exists": True,
        "human_approved_by": "the operator",
        "champion_version": "v1",
        "challenger_beat_champion": True,
    }
    base.update(overrides)
    return Evidence(**base)


def test_complete_evidence_passes():
    """The gate is not a permanent no. It passes unchanged once the evidence
    exists, which is why it is worth writing before anybody wants an answer."""

    assert gate(_good_evidence()).promoted


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("labels_are_live_verified", False, "learned the fixtures"),
        ("admitted_labels", 10, "fewer than"),
        ("folds_evaluated", 1, "one period is an anecdote"),
        ("challenger_beat_champion", False, "did not beat champion"),
        ("challenger_beat_champion", None, "no champion comparison"),
        ("calibration_exists", False, "opinion of itself"),
        ("human_approved_by", None, "no human approval"),
    ],
)
def test_each_missing_piece_refuses_on_its_own(field, value, fragment):
    decision = gate(_good_evidence(**{field: value}))

    assert not decision.promoted
    assert any(fragment in reason for reason in decision.reasons)


def test_a_near_binary_target_is_refused():
    decision = gate(
        _good_evidence(class_counts={"PREDICTIVE_SUCCESS": 100, "FALSE_POSITIVE": 100})
    )

    assert not decision.promoted
    assert any(f"Fewer than {MINIMUM_CLASSES}" in reason for reason in decision.reasons)


def test_a_thin_class_is_refused():
    """An accuracy figure over eleven examples is noise with a decimal point."""

    decision = gate(
        _good_evidence(
            class_counts={"PREDICTIVE_SUCCESS": 100, "FALSE_POSITIVE": 89, "PRICED_IN_ERROR": 11}
        )
    )

    assert not decision.promoted
    assert any("noise with a decimal point" in reason for reason in decision.reasons)


def test_every_failing_reason_is_collected_not_just_the_first():
    decision = gate(
        _good_evidence(
            labels_are_live_verified=False,
            admitted_labels=0,
            folds_evaluated=0,
            calibration_exists=False,
            human_approved_by=None,
        )
    )

    assert len(decision.reasons) >= 5


def test_today_the_gate_refuses_everything():
    """Asserted rather than left as a comment, so it stops being true loudly.

    When there are real episodes, real outcomes and real labels, this test will
    need changing - and that is the moment someone should have to think about it.
    """

    decision = describe_current_state()

    assert not decision.promoted
    assert any("real prices" in reason for reason in decision.reasons)
    assert any("no human approval" in reason for reason in decision.reasons)
