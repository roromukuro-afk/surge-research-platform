"""The watch machine, and the one edge it does not have.

``TRIGGER_HIT -> ENTERED`` is missing on purpose. A watch condition being
reached is an observation about the market; entering is a decision about it, and
CLAUDE.md 1-5 requires an analysis between the two. The path is
``TRIGGER_HIT -> IN_REANALYSIS -> ENTERED``, and the reanalysis is free to say
no - which is the entire reason for making it run.

The same table exists as a trigger on ``prod.watch_transitions``. Two copies of
a rule is usually a smell; here it is deliberate, because this one gives a clear
error in the job that is about to do the wrong thing, and the database one holds
even if some future code path forgets to come through here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from surge.entry.models import AnalysisKind, EntryError, WatchState

WATCH_MACHINE_VERSION = "watch-machine-1.0.0"

#: Every legal edge. Anything not here raises rather than being ignored: a watch
#: that silently stayed put would look like a condition that never fired.
LEGAL: dict[WatchState | None, frozenset[WatchState]] = {
    None: frozenset({WatchState.ARMED}),
    WatchState.ARMED: frozenset({WatchState.TRIGGER_HIT, WatchState.EXPIRED}),
    WatchState.TRIGGER_HIT: frozenset({WatchState.IN_REANALYSIS, WatchState.EXPIRED}),
    WatchState.IN_REANALYSIS: frozenset(
        {WatchState.ENTERED, WatchState.REJECTED, WatchState.REARMED, WatchState.EXPIRED}
    ),
    WatchState.REARMED: frozenset({WatchState.TRIGGER_HIT, WatchState.EXPIRED}),
    WatchState.REJECTED: frozenset(),
    WatchState.ENTERED: frozenset(),
    WatchState.EXPIRED: frozenset(),
}

TERMINAL = frozenset({WatchState.ENTERED, WatchState.REJECTED, WatchState.EXPIRED})


class IllegalTransition(EntryError):
    """A move the machine does not have."""


@dataclass(frozen=True)
class Transition:
    from_state: WatchState | None
    to_state: WatchState
    occurred_at: datetime
    observed_price: Decimal | None = None
    observed_price_at: datetime | None = None
    analysis_kind: AnalysisKind | None = None
    note: str | None = None


@dataclass
class Watch:
    """One condition being watched, and everywhere it has been."""

    watch_id: str
    security_id: str
    setup_id: str
    trigger_description: str
    opened_at: datetime | None = None
    trigger_price: Decimal | None = None
    expires_after: datetime | None = None
    state: WatchState = WatchState.ARMED
    history: list[Transition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.history:
            return
        if self.opened_at is None:
            raise EntryError(
                f"watch {self.watch_id} needs an opened_at; the machine's first transition is "
                "into ARMED and it has to have happened at some point"
            )
        self.history.append(
            Transition(from_state=None, to_state=WatchState.ARMED, occurred_at=self.opened_at)
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def move(
        self,
        to_state: WatchState,
        *,
        at: datetime,
        observed_price: Decimal | None = None,
        observed_price_at: datetime | None = None,
        analysis_kind: AnalysisKind | None = None,
        note: str | None = None,
    ) -> Transition:
        if to_state is WatchState.ENTERED and self.state is WatchState.TRIGGER_HIT:
            # Named separately from the generic error because this is the one
            # mistake worth recognising on sight.
            raise IllegalTransition(
                f"watch {self.watch_id}: reaching a trigger is not a decision to enter. "
                "A REANALYSIS must run first (TRIGGER_HIT -> IN_REANALYSIS -> ENTERED)"
            )
        allowed = LEGAL.get(self.state, frozenset())
        if to_state not in allowed:
            raise IllegalTransition(
                f"watch {self.watch_id}: cannot move {self.state.value} -> {to_state.value}; "
                f"legal moves are {sorted(s.value for s in allowed) or '(none - terminal)'}"
            )
        if to_state is WatchState.ENTERED and analysis_kind is not AnalysisKind.REANALYSIS:
            raise IllegalTransition(
                f"watch {self.watch_id}: entering from a watch requires a REANALYSIS, got "
                f"{analysis_kind.value if analysis_kind else 'nothing'}"
            )

        transition = Transition(
            from_state=self.state,
            to_state=to_state,
            occurred_at=at,
            observed_price=observed_price,
            observed_price_at=observed_price_at,
            analysis_kind=analysis_kind,
            note=note,
        )
        self.history.append(transition)
        self.state = to_state
        return transition

    # ----------------------------------------------------------- convenience

    def trigger(self, *, at: datetime, price: Decimal | None = None, note: str | None = None):
        """The condition was reached. Nothing has been decided."""

        return self.move(
            WatchState.TRIGGER_HIT,
            at=at,
            observed_price=price,
            observed_price_at=at,
            analysis_kind=AnalysisKind.WATCH_MONITOR,
            note=note,
        )

    def begin_reanalysis(self, *, at: datetime, note: str | None = None):
        return self.move(
            WatchState.IN_REANALYSIS, at=at, analysis_kind=AnalysisKind.REANALYSIS, note=note
        )

    def enter(self, *, at: datetime, note: str | None = None):
        return self.move(
            WatchState.ENTERED, at=at, analysis_kind=AnalysisKind.REANALYSIS, note=note
        )

    def reject(self, *, at: datetime, note: str | None = None):
        return self.move(
            WatchState.REJECTED, at=at, analysis_kind=AnalysisKind.REANALYSIS, note=note
        )

    def rearm(self, *, at: datetime, note: str | None = None):
        return self.move(
            WatchState.REARMED, at=at, analysis_kind=AnalysisKind.REANALYSIS, note=note
        )

    def expire(self, *, at: datetime, note: str | None = None):
        return self.move(WatchState.EXPIRED, at=at, note=note)


def reached_entry_legally(history: Iterable[Transition]) -> bool:
    """Did this watch get to ENTERED through a reanalysis?

    Used when auditing stored history rather than driving the machine: the
    machine cannot produce an illegal path, but a row written by some other
    route could, and this is how that would be found.
    """

    previous: WatchState | None = None
    saw_reanalysis = False
    for transition in history:
        if transition.to_state is WatchState.IN_REANALYSIS:
            saw_reanalysis = True
        if transition.to_state is WatchState.ENTERED:
            if previous is not WatchState.IN_REANALYSIS or not saw_reanalysis:
                return False
            if transition.analysis_kind is not AnalysisKind.REANALYSIS:
                return False
        previous = transition.to_state
    return True
