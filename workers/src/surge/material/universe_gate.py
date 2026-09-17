"""The universe rule, applied to the material side as well as the technical one.

The two routes stay independent - neither filters the other, and that is the
whole point of running them in parallel. But the universe definition is not one
of the routes. It is a global rule about *what this platform predicts on*, and a
rule that applied to only one side would let an ETF reach Stage 3 through a
disclosure while being excluded from every price screen.

Three decisions, three different treatments:

``INCLUDED``
    A formal candidate. Everything downstream is open to it.
``UNRESOLVED``
    The event, its relations and its features are all stored and are visible to
    research. It does **not** become a formal candidate. ``UNRESOLVED`` is not
    "excluded" - the project says so explicitly - so nothing is discarded; it is
    simply not promoted while the question is open.
``EXCLUDED``
    The event is stored. It does not reach Stage 2 or Stage 3. An ETF's daily
    disclosure is a real disclosure and a real record; it is not something this
    platform predicts on.

Absence from the universe is treated as ``UNRESOLVED`` rather than as
``EXCLUDED``. A security the master has never heard of - a listing newer than
our snapshot - is an open question, not a decided one, and deciding it the
convenient way would quietly delete new issuers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

UNIVERSE_GATE_VERSION = "material-universe-gate-1.0.0"


class UniverseDecision(StrEnum):
    """Mirrors ``universe.decision``."""

    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class GateVerdict:
    security_id: str
    decision: UniverseDecision
    reason_code: str | None
    #: May this become a formal candidate for Stage 2 and Stage 3?
    formal_candidate: bool
    #: May research see the event, its relations and its features? Always true -
    #: the gate never deletes, it only declines to promote.
    research_visible: bool
    detail: str

    @property
    def blocks_promotion(self) -> bool:
        return not self.formal_candidate


def gate(
    security_id: str,
    universe: Mapping[str, tuple[str, str | None]] | None,
) -> GateVerdict:
    """Decide what one security may do, from the authoritative universe read.

    ``universe`` maps ``security_id`` to ``(decision, reason_code)`` and should
    come from ``universe.eligibility_as_of(market, version, knowledge_cutoff)``
    - the publication-aware read, not a query against the newest run. Which run
    counts is a publication decision, and reading around it would let an
    unpublished run change what is predicted on.
    """

    if universe is None:
        # No universe read at all. Not a licence to proceed: everything is an
        # open question until the gate has actually been consulted.
        return GateVerdict(
            security_id=security_id,
            decision=UniverseDecision.UNRESOLVED,
            reason_code="UNIVERSE_NOT_CONSULTED",
            formal_candidate=False,
            research_visible=True,
            detail=(
                "no authoritative universe read was supplied, so nothing is promoted. "
                "This is a missing input rather than a finding about the security"
            ),
        )

    entry = universe.get(security_id)
    if entry is None:
        return GateVerdict(
            security_id=security_id,
            decision=UniverseDecision.UNRESOLVED,
            reason_code="NOT_IN_UNIVERSE_SNAPSHOT",
            formal_candidate=False,
            research_visible=True,
            detail=(
                "the security is not in the authoritative universe snapshot - most often a listing "
                "newer than it. Held open rather than excluded: absence is an unanswered question, "
                "not a decision"
            ),
        )

    decision_text, reason_code = entry
    decision = UniverseDecision(decision_text)

    if decision is UniverseDecision.INCLUDED:
        return GateVerdict(
            security_id=security_id,
            decision=decision,
            reason_code=reason_code,
            formal_candidate=True,
            research_visible=True,
            detail=f"in the universe ({reason_code})",
        )

    if decision is UniverseDecision.EXCLUDED:
        return GateVerdict(
            security_id=security_id,
            decision=decision,
            reason_code=reason_code,
            formal_candidate=False,
            research_visible=True,
            detail=(
                f"excluded from the universe ({reason_code}). The event is stored and readable; it does "
                "not reach Stage 2 or Stage 3, because this platform does not predict on this security"
            ),
        )

    return GateVerdict(
        security_id=security_id,
        decision=decision,
        reason_code=reason_code,
        formal_candidate=False,
        research_visible=True,
        detail=(
            f"unresolved in the universe ({reason_code}). Stored, research-visible, and not promoted "
            "while the question is open. UNRESOLVED is not a synonym for excluded"
        ),
    )


@dataclass
class GateReport:
    """What the gate did to a day's material candidates."""

    version: str = UNIVERSE_GATE_VERSION
    verdicts: dict[str, GateVerdict] = field(default_factory=dict)

    @property
    def promoted(self) -> list[str]:
        return sorted(sid for sid, v in self.verdicts.items() if v.formal_candidate)

    @property
    def held(self) -> list[str]:
        return sorted(sid for sid, v in self.verdicts.items() if not v.formal_candidate)

    @property
    def summary(self) -> dict[str, int]:
        counts = {decision.value: 0 for decision in UniverseDecision}
        for verdict in self.verdicts.values():
            counts[verdict.decision.value] += 1
        counts["promoted"] = len(self.promoted)
        counts["held_research_only"] = len(self.held)
        return counts


def apply_gate(
    security_ids: Sequence[str],
    universe: Mapping[str, tuple[str, str | None]] | None,
) -> GateReport:
    report = GateReport()
    for security_id in security_ids:
        report.verdicts[security_id] = gate(security_id, universe)
    return report
