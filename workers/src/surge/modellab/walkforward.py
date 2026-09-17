"""Walk-forward folds, with no way to build a leaking one.

CLAUDE.md 1-16 forbids a random shuffle split. This module does not have one -
not as a discouraged option, not behind a flag. The only split it can produce is
time-ordered, and every fold's test window starts after its training window ends
plus a purge gap.

The purge gap is the part that is easy to leave out and expensive to leave out.
A label here is not known on the day it is dated: an episode opened on day D has
a primary horizon running to S20, so its outcome is only settled about a month
later. Training on a label dated D-1 and testing on D would therefore be training
on something whose answer came from inside the test window. The gap has to be at
least the horizon.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

WALK_FORWARD_VERSION = "walk-forward-1.0.0"

#: The primary horizon. A label dated D is not settled until about S20, so a
#: training set that ends within this many sessions of the test window is
#: training on answers from inside it.
HORIZON_SESSIONS = 20


class LeakageError(RuntimeError):
    """A split that would train on information from its own test window."""


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: date
    train_end: date
    #: Nothing in here is used for anything. It exists so that a label dated
    #: near train_end has its outcome settled before the test window opens.
    purge_start: date
    purge_end: date
    test_start: date
    test_end: date

    def __post_init__(self) -> None:
        if self.train_start > self.train_end:
            raise LeakageError(f"fold {self.index}: the training window is inverted")
        if self.test_start > self.test_end:
            raise LeakageError(f"fold {self.index}: the test window is inverted")
        if self.purge_start <= self.train_end:
            raise LeakageError(f"fold {self.index}: the purge gap overlaps the training window")
        if self.test_start <= self.purge_end:
            raise LeakageError(
                f"fold {self.index}: the test window starts inside the purge gap, so a label from "
                "the end of training could still be settled by data the test window contains"
            )

    @property
    def purge_days(self) -> int:
        return (self.purge_end - self.purge_start).days + 1

    def contains_in_training(self, when: date) -> bool:
        return self.train_start <= when <= self.train_end

    def contains_in_test(self, when: date) -> bool:
        return self.test_start <= when <= self.test_end


def build_folds(
    *,
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    purge_days: int | None = None,
    step_days: int | None = None,
) -> list[Fold]:
    """Expanding-origin walk-forward folds over a date range.

    ``purge_days`` defaults to a calendar month, which is the ordinary length of
    twenty trading sessions. It may be made longer and not shorter: a gap under
    the horizon is a gap that does not do its job.
    """

    if train_days <= 0 or test_days <= 0:
        raise ValueError("a fold needs a non-empty training and test window")

    minimum_purge = _minimum_purge_days()
    purge = minimum_purge if purge_days is None else purge_days
    if purge < minimum_purge:
        raise LeakageError(
            f"a purge gap of {purge} day(s) is shorter than the {minimum_purge} the primary horizon "
            f"needs. A label dated at the end of training is not settled until about S{HORIZON_SESSIONS}, "
            "so a shorter gap trains on answers that came from inside the test window"
        )

    step = step_days or test_days
    folds: list[Fold] = []
    index = 0
    train_end = start + timedelta(days=train_days - 1)

    while True:
        purge_start = train_end + timedelta(days=1)
        purge_end = purge_start + timedelta(days=purge - 1)
        test_start = purge_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > end:
            break
        folds.append(
            Fold(
                index=index,
                train_start=start,
                train_end=train_end,
                purge_start=purge_start,
                purge_end=purge_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        index += 1
        train_end = train_end + timedelta(days=step)

    return folds


def _minimum_purge_days() -> int:
    """Twenty trading sessions, in calendar days, rounded up.

    Five sessions a week means twenty sessions span four weeks of trading, which
    is twenty-eight calendar days. Rounded to thirty so a run of holidays does
    not quietly shorten it.
    """

    return 30


def assert_no_leakage(fold: Fold, labels: Sequence[tuple[date, date]]) -> None:
    """Refuse a fold whose training labels are settled inside its test window.

    ``labels`` is ``(dated_on, settled_on)``: when the observation was made, and
    when its outcome actually became known. The purge gap is a calendar
    approximation of the horizon, and a calendar is not a trading calendar - a
    run of holidays, a suspension, or an episode that stayed open past S20
    stretches the real settlement date past the gap.

    The window check in :class:`Fold` cannot see that, because it only knows the
    boundaries. This one looks at the labels themselves, and it is the check that
    catches the case where the approximation was not enough.
    """

    offenders = [
        (dated_on, settled_on)
        for dated_on, settled_on in labels
        if fold.contains_in_training(dated_on) and fold.contains_in_test(settled_on)
    ]
    if offenders:
        first, settled = offenders[0]
        raise LeakageError(
            f"fold {fold.index}: {len(offenders)} training label(s) are not settled until inside "
            f"the test window (for example one dated {first} settled {settled}). The purge gap is a "
            "calendar approximation of the horizon and was not long enough here"
        )


def split(fold: Fold, rows: Sequence[tuple[date, object]]) -> tuple[list[object], list[object]]:
    """Split rows by date. There is no shuffle, and there is no option for one.

    Rows dated inside the purge gap go into neither side. That is what the gap
    is - not a third dataset, but a deliberate hole.
    """

    train = [row for when, row in rows if fold.contains_in_training(when)]
    test = [row for when, row in rows if fold.contains_in_test(when)]
    return train, test


__all__ = [
    "HORIZON_SESSIONS",
    "WALK_FORWARD_VERSION",
    "Fold",
    "LeakageError",
    "assert_no_leakage",
    "build_folds",
    "split",
]
