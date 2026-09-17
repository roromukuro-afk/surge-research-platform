"""Stage 3 answers become setups and watches.

This is the seam between the end-of-day analysis and the intraday machinery.
Two things happen here and nothing else does:

* every surviving Stage 3 answer becomes one ``prod.setups`` row, including the
  rejections. A rejected security is a decision that was made and is part of the
  denominator;
* the three ``WATCH_*`` states also arm a watch, which is what the next session's
  monitor will act on.

What does not happen here is any kind of entry. ``analysis.stage3_state`` has no
``ENTRY`` member, so there is nothing to translate even by accident - and the
setup row's own check constraint refuses the state from an end-of-day pass
regardless.

A post-close catalyst carries ``NOT_EVALUATED_AGAINST_EOD``. The close cannot
have priced in something published after it, and the next session's decision is
where that question gets asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from surge.analysis.llm import Stage3State
from surge.analysis.stage3 import Stage3Result
from surge.entry.models import AnalysisKind, DecisionState, VerificationStatus

SETUP_VERSION = "setup-from-stage3-1.0.0"

#: The three states that leave something to monitor tomorrow.
WATCH_STATES = frozenset(
    {Stage3State.WATCH_BREAKOUT, Stage3State.WATCH_PULLBACK, Stage3State.WATCH_OTHER}
)

_TRIGGERS = {
    Stage3State.WATCH_BREAKOUT: "price clears the level the breakout thesis depends on",
    Stage3State.WATCH_PULLBACK: "price holds the reference average the pullback thesis depends on",
    # WATCH_OTHER is the state the analysis reaches when it cannot assert either
    # setup - most often because whether a disclosure landed before or after the
    # close is UNKNOWN. The trigger says so rather than inventing a level.
    Stage3State.WATCH_OTHER: (
        "conditions are not specific enough to state a level; monitored so the next session's "
        "decision has the record, not because a threshold is known"
    ),
}


@dataclass(frozen=True)
class SetupRecord:
    """One ``prod.setups`` row, before it is written."""

    security_id: str
    as_of_date: date
    state: DecisionState
    analysis_kind: AnalysisKind
    price_cutoff_at: datetime
    knowledge_cutoff_at: datetime
    priced_in_status: str
    signal_reference_price: float | None = None
    signal_reference_currency: str | None = None
    thesis_key: str | None = None
    rationale: str | None = None
    verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED
    #: Filled for the three WATCH_* states, empty otherwise.
    trigger_description: str | None = None

    @property
    def arms_a_watch(self) -> bool:
        return self.trigger_description is not None


@dataclass
class SetupReport:
    as_of_date: date
    version: str = SETUP_VERSION
    setups: list[SetupRecord] = None  # type: ignore[assignment]
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.setups = self.setups or []
        self.notes = self.notes or []

    @property
    def watches(self) -> list[SetupRecord]:
        return [s for s in self.setups if s.arms_a_watch]

    @property
    def summary(self) -> dict[str, int]:
        counts = {state.value: 0 for state in DecisionState}
        for setup in self.setups:
            counts[setup.state.value] += 1
        counts["watches_armed"] = len(self.watches)
        return counts


def thesis_key_for(result: Stage3Result) -> str:
    """A provisional thesis key from what actually drove the answer.

    D-17a has not settled what "the same thesis" means, so this is explicitly a
    stand-in: the state plus the sorted routes that fired. It is stable across a
    rerun of the same day and it is recorded verbatim rather than derived again
    downstream, so replacing it later is a versioned change rather than a silent
    reinterpretation of past episodes.
    """

    routes = (result.bundle.sections.get("routes") or {}) if result.bundle else {}
    technical = sorted(routes.get("technical_routes", ()) or ())
    material = sorted(routes.get("material_routes", ()) or ())
    parts = [result.state.value, *technical, *material]
    return "|".join(parts)


def _state_for(state: Stage3State) -> DecisionState:
    # The two vocabularies are the same minus ENTRY, which stage3_state does not
    # have. A KeyError here would mean someone added it.
    return DecisionState(state.value)


def to_setups(
    results,
    *,
    as_of_date: date,
    price_cutoff_at: datetime,
    knowledge_cutoff_at: datetime,
    analysis_kind: AnalysisKind = AnalysisKind.EOD,
) -> SetupReport:
    """Turn Stage 3 answers into setup records.

    ``analysis_kind`` is ``EOD`` for the daily pass and ``POST_CLOSE_MATERIAL``
    for the evening one. It decides the ``priced_in_status``, which is the field
    that keeps a post-close disclosure from being read as something the close
    had already absorbed.
    """

    report = SetupReport(as_of_date=as_of_date)

    if analysis_kind not in (AnalysisKind.EOD, AnalysisKind.POST_CLOSE_MATERIAL):
        raise ValueError(
            f"{analysis_kind.value} does not produce setups; setups come from the two end-of-day "
            "passes, and an intraday decision produces an entry attempt instead"
        )

    for result in results:
        state = _state_for(result.state)
        is_post_close = state is DecisionState.POST_CLOSE_CATALYST_SETUP
        record = SetupRecord(
            security_id=result.security_id,
            as_of_date=as_of_date,
            state=state,
            analysis_kind=analysis_kind,
            price_cutoff_at=price_cutoff_at,
            knowledge_cutoff_at=knowledge_cutoff_at,
            priced_in_status=(
                "NOT_EVALUATED_AGAINST_EOD" if is_post_close else "EVALUATED_AGAINST_EOD"
            ),
            signal_reference_price=result.threshold_reference_price,
            thesis_key=thesis_key_for(result),
            rationale=result.response.rationale,
            trigger_description=_TRIGGERS.get(result.state),
        )
        report.setups.append(record)

    if any(s.state is DecisionState.POST_CLOSE_CATALYST_SETUP for s in report.setups):
        report.notes.append(
            "post-close catalysts carry NOT_EVALUATED_AGAINST_EOD: the close cannot have priced "
            "in something published after it, so how far it is priced in is the next session's "
            "question"
        )
    report.notes.append(
        "no entry is produced here. analysis.stage3_state has no ENTRY member and the setup row "
        "refuses it from an end-of-day pass"
    )
    return report


__all__ = ["SETUP_VERSION", "SetupRecord", "SetupReport", "thesis_key_for", "to_setups"]
