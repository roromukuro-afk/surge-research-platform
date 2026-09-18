"""The intraday entry analysis: the only analysis that may say ENTRY.

Stage 3 runs against a closed market and has no ENTRY member, which is correct
and is also not enough. Something has to run *during* the session, look at a live
price, and answer the question Stage 3 structurally cannot: is this enterable
now, at this price. Without it, a watch that reaches its trigger has nowhere to
go except an entry nobody analysed - which is exactly the shortcut CLAUDE.md 1-5
forbids.

So this module is the second analysis contract. It differs from Stage 3 in four
ways, and each difference is a rule rather than a convenience:

* **ENTRY exists here and only here.** The five states are ENTRY, the three
  WATCH_* states and REJECT. ``TECHNICAL_SETUP_EOD`` and
  ``POST_CLOSE_CATALYST_SETUP`` are absent: they are end-of-day verdicts about a
  session that has finished, and an intraday pass is not entitled to them.
* **Entry language is permitted.** The Stage 3 validator rejects a rationale
  that reads as an instruction to buy, because an end-of-day answer has no live
  price to act on. Here it is the point of the exercise.
* **The model does not hold the hard filter.** It is shown the price and told
  the rule, and it may say ENTRY on a security over 3,000 yen anyway - models do.
  The validator catches it, the Phase 8 decision catches it again, and the
  database catches it a third time. The authority is the guard, never the text.
* **A failure line is mandatory for an ENTRY.** ``initial_failure_line`` is
  fixed at prediction time and never moves (CLAUDE.md 1-6), so it has to come
  from the analysis that proposed the entry. An entry with no failure line is
  not a weaker entry; it is an unfalsifiable one.

What this module does not do is decide. It produces a validated verdict, and
:mod:`surge.entry.decision` turns a verdict into a prediction or into one of the
six statuses that do not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from surge.analysis.bundle import BundleError, canonical_json, sha256_text
from surge.analysis.llm import LLMRequest, ProviderKind, ZoneBasisKind
from surge.analysis.validate import ValidationStatus, reads_as_prior_high_upside
from surge.entry.models import (
    PRICE_LIMIT_JPY,
    AnalysisKind,
    AnalysisVerdict,
    DecisionState,
    EntryError,
    EntryRequest,
    ObservedPrice,
    UniverseVerdict,
    VerificationStatus,
    target_for,
)

ENTRY_ANALYSIS_VERSION = "entry-analysis-1.0.0"
ENTRY_BUNDLE_VERSION = "entry-bundle-1.0.0"
ENTRY_VALIDATOR_VERSION = "entry-analysis-validator-1.0.0"


class EntryAnalysisState(StrEnum):
    """The five answers an intraday pass may give.

    A deliberate subset of ``prod.decision_state``. The two setup states are not
    here: a setup is a claim about a finished session, and repeating it from
    inside the session would let an intraday pass overwrite the end-of-day
    record it is supposed to be acting on.
    """

    ENTRY = "ENTRY"
    WATCH_BREAKOUT = "WATCH_BREAKOUT"
    WATCH_PULLBACK = "WATCH_PULLBACK"
    WATCH_OTHER = "WATCH_OTHER"
    REJECT = "REJECT"

    @property
    def is_a_watch(self) -> bool:
        return self.value.startswith("WATCH_")


#: The sections an intraday bundle carries, in serialisation order. Fixed: the
#: hash is the reason the bundle exists, and reordering would change it without
#: changing anything the model saw.
ENTRY_SECTION_ORDER = (
    "security",
    "live_price",
    "fx",
    "eod_setup",
    "watch",
    "technical_features",
    "material_events",
    "stage2",
    "stage3_setup",
    "price_obstacles",
    "reachable_zone",
    "coverage",
    "open_episode",
)

#: Sections whose absence changes what the answer can mean. A missing section is
#: recorded as a warning rather than silently tolerated: an entry judged without
#: the obstacles above the price is a different judgement from one that saw them
#: and dismissed them.
SECTIONS_AN_ENTRY_NEEDS = ("live_price", "stage3_setup", "coverage")


class EntryContractError(EntryError):
    """An analysis output that may not be turned into a decision."""


@dataclass
class IntradayBundle:
    """Everything the intraday analysis sees, plus the hashes to reproduce it.

    Same discipline as the Stage 3 bundle: the canonical prompt and the addenda
    travel as hashes rather than as embedded text, so a bundle that folded a
    later decision into v5.1 would be detectable instead of invisible.
    """

    security_id: str
    market_code: str
    session_date: date
    decision_cutoff_at: datetime
    canonical_prompt_sha256: str
    addenda_sha256: list[str] = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    versions: dict = field(default_factory=dict)
    bundle_version: str = ENTRY_BUNDLE_VERSION

    def __post_init__(self) -> None:
        if len(self.canonical_prompt_sha256) != 64:
            raise BundleError("canonical_prompt_sha256 must be a full SHA-256 hex digest")
        if "canonical_prompt" in self.sections or "addenda" in self.sections:
            raise BundleError(
                "the canonical prompt and its addenda are referenced by hash, not embedded as "
                "sections; embedding them is how v5.1 and its addenda get mixed"
            )
        unknown = set(self.sections) - set(ENTRY_SECTION_ORDER)
        if unknown:
            raise BundleError(f"unknown intraday bundle sections: {sorted(unknown)}")

    @property
    def missing_sections(self) -> tuple[str, ...]:
        return tuple(name for name in SECTIONS_AN_ENTRY_NEEDS if name not in self.sections)

    @property
    def section_digests(self) -> dict[str, str]:
        digests = {
            "canonical_prompt": self.canonical_prompt_sha256,
            "addenda": sha256_text(canonical_json(sorted(self.addenda_sha256))),
        }
        for name in ENTRY_SECTION_ORDER:
            digests[name] = sha256_text(canonical_json(self.sections.get(name)))
        return digests

    def serialise(self) -> str:
        payload = {
            "bundle_version": self.bundle_version,
            "security_id": self.security_id,
            "market_code": self.market_code,
            "session_date": self.session_date,
            "decision_cutoff_at": self.decision_cutoff_at,
            "versions": self.versions,
            "sections": {name: self.sections.get(name) for name in ENTRY_SECTION_ORDER},
        }
        return canonical_json(payload)

    @property
    def bundle_sha256(self) -> str:
        return sha256_text(self.serialise())


@dataclass(frozen=True)
class EntryAnalysisResponse:
    """What the intraday model returned, before validation."""

    state: EntryAnalysisState
    rationale: str
    provider_id: str
    provider_kind: ProviderKind
    model_id: str | None = None
    #: The price the model says it judged against. Echoed back so the system can
    #: check the model used the price it was shown rather than one it recalled,
    #: inferred or invented.
    decision_price_used: Decimal | None = None
    #: Where the entry stops being the thesis it was entered on. Fixed forever at
    #: prediction time, so it has to come from here.
    proposed_initial_failure_line: Decimal | None = None
    reachable_zone_low: Decimal | None = None
    reachable_zone_high: Decimal | None = None
    reachable_zone_basis_kinds: tuple[ZoneBasisKind, ...] = ()
    reachable_zone_basis: str | None = None
    concepts_considered: tuple[str, ...] = ()
    #: For the three WATCH_* states: what would have to happen next.
    watch_trigger_description: str | None = None
    reject_reason: str | None = None
    raw_text: str = ""

    @property
    def response_sha256(self) -> str:
        return hashlib.sha256((self.raw_text or self.rationale).encode("utf-8")).hexdigest()

    @property
    def is_a_stand_in(self) -> bool:
        return self.provider_kind is ProviderKind.DETERMINISTIC_MOCK


@dataclass(frozen=True)
class EntryGuardFacts:
    """The authoritative facts, none of which come from the model.

    Everything here is measured or looked up by this system. The model is shown
    the same things, and where the two disagree the model is wrong by
    construction - which is the only arrangement under which a language model can
    be allowed near a 3,000 yen rule at all.

    ``decision_completed_at`` is deliberately *not* here. It used to be, which
    meant a caller declared when the decision finished before the decision had
    started - and since an entry price is only usable if it was observed after
    that moment, a caller could have moved the line that decides which prices
    count. It is now the runner's own clock, taken when the answer has arrived
    and passed validation, and it is recorded on the execution rather than
    supplied to it.
    """

    universe: UniverseVerdict
    decision_price: ObservedPrice | None
    decision_cutoff_at: datetime
    coverage_meets_requirements: bool = False
    coverage_detail: str = "coverage was not assessed"
    open_episode_thesis_key: str | None = None
    price_limit_jpy: Decimal = PRICE_LIMIT_JPY


@dataclass
class EntryValidation:
    """Two kinds of finding, and only one of them stops the handover.

    ``errors`` are malformed answers: no failure line, a price the model did not
    actually judge against, a zone derived from the threshold. An answer like
    that cannot become a decision, and it must not become a quieter one either -
    recording a malformed ENTRY as a REJECT would put a verdict in the ledger
    that the analysis never made.

    ``system_refusals`` are well-formed answers the system will not act on: the
    security is not INCLUDED, the price is over 3,000 yen. These are findings
    about the security rather than about the answer, and they belong in the entry
    ledger - six of its seven statuses exist precisely to hold them. So they are
    recorded here and passed on, and :func:`surge.entry.decision.decide` writes
    the row. Blocking on them would make the denominator of every hit rate quietly
    wrong.
    """

    status: ValidationStatus
    errors: list[str] = field(default_factory=list)
    system_refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validator_version: str = ENTRY_VALIDATOR_VERSION

    @property
    def passed(self) -> bool:
        return self.status is not ValidationStatus.REJECTED

    @property
    def may_become_a_prediction(self) -> bool:
        """Whether this answer may be handed to the decision function.

        Not whether it will produce a prediction: a system refusal passes here
        and is then refused by the decision, with a ledger row to show for it.
        """

        return not self.errors


class EntryAnalysisProvider(Protocol):
    provider_id: str
    provider_kind: ProviderKind
    model_id: str | None

    def analyse_entry(self, request: LLMRequest) -> EntryAnalysisResponse: ...


# --------------------------------------------------------------- the prompt


def render_entry_prompt(
    bundle: IntradayBundle,
    canonical_text: str,
    addenda_texts=(),
    *,
    price_limit_jpy: Decimal = PRICE_LIMIT_JPY,
) -> str:
    """Build the text the intraday model is shown.

    The output contract states the hard filter explicitly even though the model
    has no authority over it. Telling it the rule makes a violation informative -
    an ENTRY over the limit is then a fact about the model's judgement rather
    than about its ignorance - and it costs nothing.
    """

    blocks = [
        "=== CANONICAL v5.1 (immutable) ===",
        canonical_text,
        "",
        "=== POST-v5.1 ADDENDA (later decisions; newer overrides older) ===",
    ]
    blocks.extend(addenda_texts or ["(none)"])
    blocks.extend(
        [
            "",
            "=== BUNDLE (data; decision cutoff "
            + bundle.decision_cutoff_at.isoformat()
            + ") ===",
            bundle.serialise(),
            "",
            "=== OUTPUT CONTRACT (intraday entry decision) ===",
            "Return one state from: " + ", ".join(s.value for s in EntryAnalysisState) + ".",
            "This analysis runs during the session against a live price, so ENTRY is available "
            "and an instruction to enter is an acceptable rationale.",
            "Echo back, as decision_price_used, the live price you judged against. It must be the "
            "price given in the bundle; do not substitute a remembered or inferred one.",
            "If you return ENTRY you must also return proposed_initial_failure_line: the price at "
            "which this entry's thesis is wrong. It is fixed permanently at prediction time and "
            "must be below the price you judged against.",
            f"A security whose price is above {price_limit_jpy} JPY is out of scope. You are not "
            "the authority on this: the system re-checks it and will refuse the entry regardless "
            "of what you return.",
            "The +20% threshold is arithmetic on the entry price. It is not a target you choose "
            "and it is not the top of the reachable zone; do not derive one from the other.",
            "If you give a reachable zone, justify it from one or more of: "
            + ", ".join(kind.value for kind in ZoneBasisKind)
            + ". A prior high is an obstacle, never a reason to expect a rise.",
            "For a WATCH_* state, give watch_trigger_description: what would have to happen for "
            "this to be looked at again.",
        ]
    )
    return "\n".join(blocks)


# ------------------------------------------------------------ the validator


def validate_entry_analysis(
    response: EntryAnalysisResponse,
    facts: EntryGuardFacts,
    *,
    bundle: IntradayBundle | None = None,
) -> EntryValidation:
    """Check one intraday answer against the facts the system measured itself.

    Only an ENTRY is held to the full contract. A REJECT with no failure line is
    complete; an ENTRY with no failure line is not a decision anyone could be
    judged against later.
    """

    result = EntryValidation(status=ValidationStatus.PASSED)
    rationale = response.rationale or ""

    if not rationale.strip():
        result.errors.append(
            "no rationale; a state with no reasoning cannot be reviewed or disputed later"
        )

    if reads_as_prior_high_upside(rationale):
        result.errors.append(
            "the rationale uses a prior high as upside. A price the security once traded at is "
            "where sellers are waiting (CLAUDE.md 1-11); it belongs in the obstacles, not in the "
            "case for a rise"
        )

    if response.state.is_a_watch and not (response.watch_trigger_description or "").strip():
        result.warnings.append(
            "a watch state with no trigger description; the next session has nothing to monitor"
        )

    if bundle is not None:
        for section in bundle.missing_sections:
            result.warnings.append(
                f"section '{section}' was not in the bundle, so the answer was formed without it"
            )

    if response.state is not EntryAnalysisState.ENTRY:
        result.status = ValidationStatus.REJECTED if result.errors else ValidationStatus.PASSED
        return result

    # --------------------------------------------------- an ENTRY specifically

    price = facts.decision_price
    if price is None:
        result.errors.append(
            "ENTRY without a decision price. There is no price the decision was taken against, so "
            "there is nothing to reproduce and nothing to judge"
        )
    else:
        if response.decision_price_used is None:
            result.errors.append(
                "ENTRY without decision_price_used. The model must state the price it judged "
                "against so it can be checked against the price it was shown"
            )
        elif response.decision_price_used != price.amount:
            result.errors.append(
                f"the analysis says it judged against {response.decision_price_used} but the price "
                f"it was shown was {price.amount}. A decision taken against a different number is "
                "not the decision this system can reproduce"
            )
        if price.observed_at > facts.decision_cutoff_at:
            result.errors.append(
                f"the decision price was observed at {price.observed_at.isoformat()}, after the "
                f"cutoff {facts.decision_cutoff_at.isoformat()}; the model could not have seen it"
            )
        # Reported, never enforced here, and never a reason to withhold the
        # answer from the decision function: the ledger needs the row. The limit
        # is enforced by the decision and by the database, twice more.
        if price.jpy > facts.price_limit_jpy:
            result.system_refusals.append(
                f"ENTRY on a decision price of {price.jpy} JPY, above the {facts.price_limit_jpy} "
                "limit. The analysis has no authority over the hard filter; the decision function "
                "will refuse this and record REJECTED_HARD_FILTER_AT_DECISION"
            )

    failure_line = response.proposed_initial_failure_line
    if failure_line is None:
        result.errors.append(
            "ENTRY without an initial failure line. It is fixed at prediction time and never moves "
            "(CLAUDE.md 1-6), so an entry that does not name one cannot be shown to be wrong"
        )
    elif price is not None and failure_line >= price.amount:
        result.errors.append(
            f"the initial failure line {failure_line} is not below the price judged against "
            f"({price.amount}); a failure line at or above entry is hit the moment it is set"
        )

    if response.reachable_zone_high is None:
        result.errors.append(
            "ENTRY without a reachable zone. The entry rests on the price being able to get "
            "somewhere, and where that is has to be stated to be checked afterwards"
        )
    else:
        if not response.reachable_zone_basis_kinds:
            result.errors.append(
                "a reachable zone with no basis kind; the zone is a number with no argument behind it"
            )
        if not (response.reachable_zone_basis or "").strip():
            result.errors.append("a reachable zone with no written basis")
        if (
            response.reachable_zone_low is not None
            and response.reachable_zone_high < response.reachable_zone_low
        ):
            result.errors.append("the reachable zone is inverted")
        if price is not None:
            threshold = target_for(price.amount)
            if response.reachable_zone_high == threshold:
                result.errors.append(
                    f"the reachable zone's upper bound and the +20% threshold are the same number "
                    f"({threshold}). One has been derived from the other, which collapses the "
                    "separation between arithmetic and judgement"
                )
            if response.reachable_zone_high < price.amount:
                result.warnings.append(
                    f"the zone's upper bound ({response.reachable_zone_high}) is below the price "
                    f"judged against ({price.amount}), which is an unusual basis for an entry"
                )

    if not facts.universe.may_predict:
        result.system_refusals.append(
            f"ENTRY on a security the universe calls {facts.universe.decision}. Only INCLUDED may "
            "become a formal prediction; UNRESOLVED stays in the research record and out of the "
            "results. The decision function will record REJECTED_NOT_IN_UNIVERSE"
        )

    if not facts.coverage_meets_requirements:
        # An error rather than a system refusal, and deliberately so. Coverage is
        # a precondition of running the analysis at all, not a finding about the
        # security: an entry decided on partial inputs is not comparable with one
        # decided on complete inputs, and afterwards nothing distinguishes them.
        # The job checks this before calling the model, so reaching it here means
        # a caller skipped that step.
        result.errors.append(
            f"ENTRY without the coverage an entry requires: {facts.coverage_detail}"
        )

    if response.is_a_stand_in:
        result.errors.append(
            "ENTRY from a deterministic stand-in. It exists to prove the pipeline runs and says "
            "nothing about this security; a formal prediction must not come from one"
        )

    result.status = ValidationStatus.REJECTED if result.errors else ValidationStatus.PASSED
    return result


# -------------------------------------------------- handing over to Phase 8


def to_entry_request(
    response: EntryAnalysisResponse,
    facts: EntryGuardFacts,
    *,
    security_id: str,
    thesis_key: str,
    bundle: IntradayBundle,
    analysis_kind: AnalysisKind = AnalysisKind.REANALYSIS,
    decision_completed_at: datetime,
    entry_price: ObservedPrice | None = None,
    entry_price_method: str | None = None,
    setup_ids: tuple[str, ...] = (),
    watch_id: str | None = None,
    open_episode=None,
    run_id: str | None = None,
    prompt_sha256: str | None = None,
    verification: VerificationStatus = VerificationStatus.IMPLEMENTED_NOT_LIVE_VERIFIED,
    validation: EntryValidation | None = None,
) -> EntryRequest:
    """Turn a validated intraday answer into the Phase 8 decision's input.

    Refuses an output that did not pass validation. That refusal is the join
    between the two halves: everything Phase 8 guarantees about a prediction
    rests on the verdict it was handed being one that met the contract, and the
    only way to be sure of that is for a failed validation to have no path here.
    """

    if analysis_kind not in (AnalysisKind.ENTRY_DECISION, AnalysisKind.REANALYSIS):
        raise EntryContractError(
            f"{analysis_kind.value} is not an intraday analysis; only ENTRY_DECISION and "
            "REANALYSIS run against a live price"
        )

    check = validation or validate_entry_analysis(response, facts, bundle=bundle)
    if not check.may_become_a_prediction:
        raise EntryContractError(
            f"the intraday analysis for {security_id} did not meet the entry contract and must not "
            "be turned into a decision: " + "; ".join(check.errors)
        )

    return EntryRequest(
        security_id=security_id,
        thesis_key=thesis_key,
        analysis=AnalysisVerdict(
            kind=analysis_kind,
            state=DecisionState(response.state.value),
            provider_id=response.provider_id,
            provider_kind=response.provider_kind.value,
            model_id=response.model_id,
            prompt_sha256=prompt_sha256,
            bundle_sha256=bundle.bundle_sha256,
            canonical_prompt_sha256=bundle.canonical_prompt_sha256,
            rationale=response.rationale,
        ),
        universe=facts.universe,
        decision_cutoff_at=facts.decision_cutoff_at,
        decision_completed_at=decision_completed_at,
        decision_price=facts.decision_price,
        entry_price=entry_price,
        entry_price_method=entry_price_method,
        initial_failure_line=response.proposed_initial_failure_line,
        setup_ids=setup_ids,
        watch_id=watch_id,
        open_episode=open_episode,
        run_id=run_id,
        verification=verification,
    )


# --------------------------------------------------------------- stand-in


class DeterministicIntradayStandIn:
    """A rule-based intraday stand-in, and it can never produce a prediction.

    It exists so the TRIGGER_HIT -> REANALYSIS -> outcome path can be exercised
    end to end without a credential. Its ENTRY answers are refused by the
    validator, by :func:`surge.entry.decision.decide` and by the database, in
    that order - three refusals for one rule, because the alternative is a
    mock-derived prediction sitting in the results table looking exactly like a
    real one.
    """

    provider_id = "deterministic_mock"
    provider_kind = ProviderKind.DETERMINISTIC_MOCK
    model_id = None
    version = "deterministic-intraday-stand-in-1.0.0"

    def analyse_entry(self, request: LLMRequest) -> EntryAnalysisResponse:
        bundle = request.bundle
        sections = getattr(bundle, "sections", {}) or {}
        live = sections.get("live_price") or {}
        watch = sections.get("watch") or {}
        price = live.get("price")

        if price is None:
            state = EntryAnalysisState.WATCH_OTHER
            reason = "no live price in the bundle, so nothing can be judged against"
        elif watch.get("trigger_hit"):
            state = EntryAnalysisState.WATCH_BREAKOUT
            reason = "the watch trigger was reached; a stand-in does not decide what to do about it"
        else:
            state = EntryAnalysisState.REJECT
            reason = "no trigger and no rule fired"

        raw = canonical_json(
            {
                "provider": self.provider_id,
                "version": self.version,
                "state": state.value,
                "bundle_sha256": getattr(bundle, "bundle_sha256", None),
            }
        )
        return EntryAnalysisResponse(
            state=state,
            rationale=f"[deterministic stand-in {self.version}: rules, not judgement] {reason}",
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            model_id=self.model_id,
            decision_price_used=Decimal(str(price)) if price is not None else None,
            watch_trigger_description=watch.get("trigger_description"),
            reject_reason=reason if state is EntryAnalysisState.REJECT else None,
            raw_text=raw,
        )


__all__ = [
    "ENTRY_ANALYSIS_VERSION",
    "ENTRY_BUNDLE_VERSION",
    "ENTRY_SECTION_ORDER",
    "ENTRY_VALIDATOR_VERSION",
    "DeterministicIntradayStandIn",
    "EntryAnalysisProvider",
    "EntryAnalysisResponse",
    "EntryAnalysisState",
    "EntryContractError",
    "EntryGuardFacts",
    "EntryValidation",
    "IntradayBundle",
    "render_entry_prompt",
    "to_entry_request",
    "validate_entry_analysis",
]
