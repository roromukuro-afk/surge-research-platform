"""The intraday entry decision.

Everything upstream produces analysis. This is the one function that can turn an
analysis into a prediction, which makes it the place where three prohibitions
have to hold absolutely:

* **no prediction from a missing price.** A prediction is scored against
  ``entry_reference_price`` and nothing else, so an entry without one is not a
  weaker prediction, it is not a prediction. It is recorded as
  ``NO_ENTRY_REFERENCE_PRICE`` (D-31) and goes no further.
* **no prediction from a stand-in.** The deterministic mock exists so the
  pipeline can run without a paid model. Its verdicts are evidence that the
  plumbing works and no evidence about a security. Passing one here raises
  rather than producing a row, because a mock-derived prediction in the results
  table is indistinguishable from a real one a year later.
* **no prediction outside the universe.** ``INCLUDED`` only. ``UNRESOLVED`` is
  not a synonym for excluded - the security stays in the research record and in
  the price fetch set - but it is not a synonym for included either, and the
  formal claim is what gets closed off.

The 3,000 yen filter runs twice, against two different prices, because they are
two different questions. ``decision_price`` asks whether the model should have
been considering this security at all. ``entry_reference_price`` asks whether
what could actually have been bought was inside the rule. Passing the first and
failing the second is a real case with its own status, and the episode is not
created: CLAUDE.md 1-4.

Every path writes exactly one ``EntryAttempt``. The six statuses that produce no
prediction are the denominator of any honest hit rate.
"""

from __future__ import annotations

from decimal import Decimal

from surge.entry.models import (
    ENTRY_RULE_VERSION,
    INTRADAY_KINDS,
    PRICE_LIMIT_JPY,
    AnalysisKind,
    DecisionState,
    EntryAttempt,
    EntryAttemptStatus,
    EntryError,
    EntryOutcome,
    EntryRequest,
    MissingPriceError,
    ObservedPrice,
    Prediction,
    StandInProviderError,
    target_for,
)

DECISION_VERSION = ENTRY_RULE_VERSION


def _attempt(
    request: EntryRequest,
    status: EntryAttemptStatus,
    *,
    reject_reason: str | None = None,
    include_entry_price: bool = True,
) -> EntryAttempt:
    return EntryAttempt(
        security_id=request.security_id,
        status=status,
        analysis_kind=request.analysis.kind,
        decision_cutoff_at=request.decision_cutoff_at,
        decision_completed_at=request.decision_completed_at,
        decision_price=request.decision_price,
        entry_price=request.entry_price if include_entry_price else None,
        entry_price_method=request.entry_price_method if include_entry_price else None,
        universe_decision=request.universe.decision,
        universe_reason_code=request.universe.reason_code,
        thesis_key=request.thesis_key,
        reject_reason=reject_reason,
        provider_id=request.analysis.provider_id,
        setup_id=request.setup_ids[0] if request.setup_ids else None,
        watch_id=request.watch_id,
        run_id=request.run_id,
        verification=request.verification,
    )


def decide(request: EntryRequest) -> EntryOutcome:
    """Run one entry decision and return what it produced.

    The order of the checks is part of the rule. The universe question is asked
    before any price is looked at, because a security this platform does not
    predict on should not have a price fetched for it in the first place; the
    hard filter is asked before the episode check, because an over-limit price
    is a fact about the decision rather than about the episode; and the entry
    price is only looked at once everything else has passed, because observing
    it is the expensive step and a reaffirmation does not need it.
    """

    analysis = request.analysis
    notes: list[str] = []

    # --- Preconditions. These are faults in the call, not findings about the
    # --- security, so none of them writes a ledger row.
    if analysis.kind not in INTRADAY_KINDS:
        raise EntryError(
            f"{analysis.kind.value} cannot produce an entry: an end-of-day pass runs against a "
            "closed market, where 'enterable at the current price' has no referent (CLAUDE.md 1-4)"
        )
    if request.decision_completed_at < request.decision_cutoff_at:
        raise EntryError(
            "the decision finished before its own data cutoff, which cannot have happened: "
            f"completed {request.decision_completed_at.isoformat()} < cutoff "
            f"{request.decision_cutoff_at.isoformat()}"
        )
    if analysis.is_a_stand_in:
        raise StandInProviderError(
            f"provider {analysis.provider_id!r} is a deterministic stand-in. It exists to prove the "
            "pipeline runs and says nothing about this security; a formal prediction must not come "
            "from one"
        )

    # --- Findings. Each writes exactly one ledger row.
    if analysis.state is not DecisionState.ENTRY:
        return EntryOutcome(
            attempt=_attempt(
                request,
                EntryAttemptStatus.REJECTED_BY_ANALYSIS,
                reject_reason=f"the analysis returned {analysis.state.value}, not ENTRY",
            ),
            notes=("no entry was proposed",),
        )

    if not request.universe.may_predict:
        return EntryOutcome(
            attempt=_attempt(
                request,
                EntryAttemptStatus.REJECTED_NOT_IN_UNIVERSE,
                reject_reason=(
                    f"universe decision is {request.universe.decision}"
                    f"{f' ({request.universe.reason_code})' if request.universe.reason_code else ''}; "
                    "only INCLUDED may become a formal prediction"
                ),
                include_entry_price=False,
            ),
            notes=(
                "UNRESOLVED is not excluded: the event, the features and the research record all "
                "remain. Only the formal claim is closed off",
            ),
        )

    if request.decision_price is None:
        raise MissingPriceError(
            f"{request.security_id}: an entry decision needs a price to have been taken against. "
            "Without decision_price there is no decision to record, and inventing one would put a "
            "fabricated number in the audit trail"
        )

    decision_price = request.decision_price
    decision_price.check_fx_not_after(request.decision_cutoff_at, label="decision price")
    if decision_price.observed_at > request.decision_cutoff_at:
        raise EntryError(
            f"{request.security_id}: the decision price was observed at "
            f"{decision_price.observed_at.isoformat()}, after the cutoff "
            f"{request.decision_cutoff_at.isoformat()} - the model could not have seen it"
        )

    # First of the two limit checks (CLAUDE.md 1-4). Whether the security was
    # under the limit at the close is not the question being asked here.
    if decision_price.jpy > PRICE_LIMIT_JPY:
        return EntryOutcome(
            attempt=_attempt(
                request,
                EntryAttemptStatus.REJECTED_HARD_FILTER_AT_DECISION,
                reject_reason=(
                    f"decision price {decision_price.jpy} JPY is over the {PRICE_LIMIT_JPY} limit "
                    "(HARD_FILTER_AT_ENTRY)"
                ),
                include_entry_price=False,
            ),
        )

    # Same security, same thesis, already running. One episode, one claim: a
    # second prediction here would make repeating yourself look like being right
    # twice.
    if request.open_episode is not None:
        if request.open_episode.thesis_key != request.thesis_key:
            raise EntryError(
                f"{request.security_id}: the open episode is under thesis "
                f"{request.open_episode.thesis_key!r} but this decision is under "
                f"{request.thesis_key!r}; a different thesis is a different episode (D-17b)"
            )
        return EntryOutcome(
            attempt=_attempt(
                request,
                EntryAttemptStatus.REAFFIRMED_EXISTING_EPISODE,
                reject_reason=(
                    f"episode {request.open_episode.episode_id} is already open under this thesis"
                ),
                include_entry_price=False,
            ),
            reaffirmed_episode_id=request.open_episode.episode_id,
            notes=(
                "recorded as REAFFIRMED. The horizon still runs from the original entry and is "
                "not restarted (CLAUDE.md 1-6)",
            ),
        )

    # D-31. The decision happened; the price it would have been taken at did not
    # arrive. That is a real event with its own row, and not an entry.
    if request.entry_price is None:
        return EntryOutcome(
            attempt=_attempt(request, EntryAttemptStatus.NO_ENTRY_REFERENCE_PRICE),
            notes=(
                "entry was judged but no tradeable price could be observed; no prediction and no "
                "episode, because the only price the scoring may use is missing",
            ),
        )

    entry_price = request.entry_price
    entry_price.check_fx_not_after(entry_price.observed_at, label="entry price")
    if entry_price.observed_at < request.decision_completed_at:
        raise EntryError(
            f"{request.security_id}: the entry price was observed at "
            f"{entry_price.observed_at.isoformat()}, before the decision finished at "
            f"{request.decision_completed_at.isoformat()}. A price the analysis could have seen is "
            "not a price it could have acted on"
        )

    # Second limit check, against the price that could actually have been paid.
    if entry_price.jpy > PRICE_LIMIT_JPY:
        return EntryOutcome(
            attempt=_attempt(request, EntryAttemptStatus.ENTRY_ABORTED_PRICE_LIMIT),
            notes=(
                f"decision price {decision_price.jpy} JPY passed, entry price {entry_price.jpy} JPY "
                f"did not. No prediction and no episode. If it falls back under "
                f"{PRICE_LIMIT_JPY} this is not revived: it needs a new decision",
            ),
        )

    failure_line = request.initial_failure_line
    if failure_line is None:
        raise EntryError(
            f"{request.security_id}: a prediction needs an initial_failure_line. It is fixed at "
            "creation and is what the primary outcome is judged against, so there is no default "
            "that would be safe to assume"
        )
    if failure_line >= entry_price.amount:
        raise EntryError(
            f"{request.security_id}: initial_failure_line {failure_line} is not below the entry "
            f"price {entry_price.amount}; a failure line at or above entry is hit immediately"
        )

    prediction = Prediction(
        security_id=request.security_id,
        thesis_key=request.thesis_key,
        analysis_kind=analysis.kind,
        entry_reference_price=entry_price.amount,
        entry_price_observed_at=entry_price.observed_at,
        entry_price_currency=entry_price.currency,
        entry_price_jpy=entry_price.jpy,
        entry_price_method=request.entry_price_method,
        decision_price=decision_price.amount,
        decision_price_observed_at=decision_price.observed_at,
        decision_price_jpy=decision_price.jpy,
        initial_failure_line=failure_line,
        # Of the entry price, always. CLAUDE.md 1-4.
        target_price=target_for(entry_price.amount),
        data_cutoff=request.decision_cutoff_at,
        provider_id=analysis.provider_id,
        provider_kind=analysis.provider_kind,
        model_id=analysis.model_id,
        prompt_sha256=analysis.prompt_sha256,
        bundle_sha256=analysis.bundle_sha256,
        canonical_prompt_sha256=analysis.canonical_prompt_sha256,
        universe_decision=request.universe.decision,
        source_setup_ids=request.setup_ids,
        verification=request.verification,
        rule_version=DECISION_VERSION,
    )
    notes.append(
        f"target {prediction.target_price} is +20% of the entry price, not of the decision price"
    )
    return EntryOutcome(
        attempt=_attempt(request, EntryAttemptStatus.PREDICTION_CREATED),
        prediction=prediction,
        notes=tuple(notes),
    )


def jpy(amount: Decimal, currency: str, fx_rate: Decimal, observed_at, fx_observed_at=None):
    """Build an :class:`ObservedPrice`, with the FX time carried rather than dropped."""

    return ObservedPrice(
        amount=amount,
        currency=currency,
        observed_at=observed_at,
        fx_rate=fx_rate if currency != "JPY" else Decimal(1),
        fx_observed_at=fx_observed_at,
    )


__all__ = ["DECISION_VERSION", "AnalysisKind", "decide", "jpy"]
