"""Freezing an analysis's input, and proving a resumed one is the same input.

Two functions, and the second is why the first exists.

:func:`build_stored_input` renders the prompt and takes its hash *before the
model is called*, so what the analysis was asked is on disk from the moment it
started rather than reconstructed afterwards from whatever the session looks
like now.

:func:`reconstruct` reads that record back, rebuilds the bundle and the prompt,
and requires the hash to match. A mismatch is not something to repair: it means
the thing being resumed is not the thing that was started, and the honest
response is to fail the analysis (``INPUT_RECONSTRUCTION_MISMATCH``) rather than
to answer a different question under the original trigger's name.

The hash is a good proof because the prompt contains everything: the canonical
v5.1 text, the addenda in order, the serialised bundle and the output contract.
Anything that changed - a section added, a decimal formatted differently, an
addendum published since, the canonical file edited - moves it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from surge.analysis.bundle import sha256_text
from surge.analysis.entry_analysis import (
    PRICE_LIMIT_JPY,
    EntryGuardFacts,
    IntradayBundle,
    render_entry_prompt,
)
from surge.analysis.execution import StoredInput
from surge.entry.models import ObservedPrice, UniverseVerdict


class InputReconstructionError(RuntimeError):
    """The stored input no longer rebuilds into the prompt that was sent."""


def build_stored_input(
    *,
    bundle: IntradayBundle,
    facts: EntryGuardFacts,
    canonical_text: str,
    addenda_texts: tuple[str, ...] = (),
    thesis_key: str | None = None,
    setup_ids: tuple[str, ...] = (),
    fx_rate=None,
    fx_observed_at: datetime | None = None,
) -> tuple[StoredInput, str]:
    """The record and the prompt it is the record of.

    Returns both so the caller cannot render a *second* prompt to send: the one
    that was hashed is the one that goes out. Rendering twice would usually
    produce the same text, and "usually" is the whole problem - the failure
    would be invisible until a recovery pass disagreed with a request nobody
    could re-examine.
    """

    prompt = render_entry_prompt(
        bundle, canonical_text, addenda_texts, price_limit_jpy=facts.price_limit_jpy
    )
    price = facts.decision_price
    stored = StoredInput(
        bundle_serialized=bundle.serialise(),
        prompt_sha256=sha256_text(prompt),
        bundle_sha256=bundle.bundle_sha256,
        canonical_prompt_sha256=bundle.canonical_prompt_sha256,
        addenda_sha256=tuple(bundle.addenda_sha256),
        thesis_key=thesis_key,
        setup_ids=tuple(setup_ids),
        decision_price=price.amount if price else None,
        decision_price_currency=price.currency if price else None,
        decision_price_observed_at=price.observed_at if price else None,
        fx_rate=fx_rate,
        fx_observed_at=fx_observed_at,
        price_limit_jpy=facts.price_limit_jpy,
        universe_decision=facts.universe.decision,
        universe_reason_code=facts.universe.reason_code,
        coverage_meets_requirements=facts.coverage_meets_requirements,
        coverage_detail=facts.coverage_detail,
    )
    return stored, prompt


@dataclass(frozen=True)
class Reconstruction:
    """What a stored input rebuilds into, once it has been proved to."""

    bundle: IntradayBundle
    facts: EntryGuardFacts
    prompt: str
    thesis_key: str | None


def rebuild_bundle(stored: StoredInput) -> IntradayBundle:
    """The bundle, from the text that was hashed.

    Sections whose stored value is null are dropped rather than kept as nulls:
    :meth:`IntradayBundle.serialise` writes every known section name and fills
    the absent ones with null, so a null here means "was not present". If that
    reading is ever wrong the prompt hash will not match, which is the point of
    checking it rather than trusting this.
    """

    payload = json.loads(stored.bundle_serialized)
    return IntradayBundle(
        security_id=payload["security_id"],
        market_code=payload["market_code"],
        session_date=date.fromisoformat(payload["session_date"]),
        decision_cutoff_at=datetime.fromisoformat(payload["decision_cutoff_at"]),
        canonical_prompt_sha256=stored.canonical_prompt_sha256,
        addenda_sha256=list(stored.addenda_sha256),
        sections={
            name: value for name, value in (payload.get("sections") or {}).items()
            if value is not None
        },
        versions=payload.get("versions") or {},
        bundle_version=payload["bundle_version"],
    )


def rebuild_facts(stored: StoredInput) -> EntryGuardFacts:
    """The guard facts as they were at cutoff, not as they are now.

    Re-measuring them would answer a different question: the universe verdict
    and the coverage assessment are statements about a moment, and the moment
    has passed.
    """

    price = None
    if stored.decision_price is not None:
        price = ObservedPrice(
            amount=stored.decision_price,
            currency=stored.decision_price_currency or "JPY",
            observed_at=stored.decision_price_observed_at,
        )
    bundle = rebuild_bundle(stored)
    return EntryGuardFacts(
        universe=UniverseVerdict(
            decision=stored.universe_decision or "UNRESOLVED",
            reason_code=stored.universe_reason_code,
        ),
        decision_price=price,
        decision_cutoff_at=bundle.decision_cutoff_at,
        coverage_meets_requirements=bool(stored.coverage_meets_requirements),
        coverage_detail=stored.coverage_detail or "coverage was not assessed",
        price_limit_jpy=stored.price_limit_jpy or PRICE_LIMIT_JPY,
    )


def reconstruct(
    stored: StoredInput, *, canonical_text: str, addenda_texts: tuple[str, ...] = ()
) -> Reconstruction:
    """Rebuild the input and prove it is the one that was sent.

    The canonical text and the addenda come from the caller because they are
    files, not rows; the stored hashes say which files, and the prompt hash says
    the whole thing rebuilt identically. So an addendum published since, or a
    canonical file that was edited, fails here rather than silently changing
    what a resumed analysis is answering.
    """

    bundle = rebuild_bundle(stored)
    facts = rebuild_facts(stored)
    prompt = render_entry_prompt(
        bundle, canonical_text, addenda_texts, price_limit_jpy=facts.price_limit_jpy
    )
    rebuilt = sha256_text(prompt)

    if rebuilt != stored.prompt_sha256:
        raise InputReconstructionError(
            "the stored input no longer rebuilds into the prompt that was sent: recorded "
            f"{stored.prompt_sha256}, rebuilt {rebuilt}. This analysis cannot be resumed as "
            "itself, and answering a different prompt under the original trigger would attribute "
            "a decision to inputs it never saw"
        )
    if bundle.bundle_sha256 != stored.bundle_sha256:
        raise InputReconstructionError(
            "the stored bundle does not rebuild into its own hash: recorded "
            f"{stored.bundle_sha256}, rebuilt {bundle.bundle_sha256}"
        )

    return Reconstruction(
        bundle=bundle, facts=facts, prompt=prompt, thesis_key=stored.thesis_key
    )


__all__ = [
    "InputReconstructionError",
    "Reconstruction",
    "build_stored_input",
    "rebuild_bundle",
    "rebuild_facts",
    "reconstruct",
]
