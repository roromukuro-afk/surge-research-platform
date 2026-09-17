"""The output validator: what a Stage 3 answer is not allowed to be.

A model that is asked for a state and a justification will sometimes return
something adjacent to what was asked for. The validator's job is to catch the
cases where "adjacent" means "breaks a rule the project exists to enforce", and
to record the reason rather than silently correcting it.

Three of these are not stylistic:

* **No entry decision.** The state enum has no ENTRY member, so a response
  claiming one cannot be stored - but a rationale that reads as an entry
  instruction is caught here, because the text is what a person will read.
* **A reachable zone needs a permitted basis.** And prior highs are not among
  them: a price the stock once traded at is where sellers wait, not a reason it
  returns.
* **The 20% threshold is arithmetic, not a target.** If the zone and the
  threshold are the same number, one of them has been derived from the other,
  and the separation the project asked for has quietly collapsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from surge.analysis.llm import LLMResponse, Stage3State, ZoneBasisKind

VALIDATOR_VERSION = "stage3-validator-1.0.0"


class ValidationStatus(StrEnum):
    PASSED = "PASSED"
    REPAIRED = "REPAIRED"
    REJECTED = "REJECTED"


#: Phrases that turn an analysis into an instruction. An end-of-day answer may
#: describe conditions; it may not tell anyone to buy now.
_ENTRY_LANGUAGE = re.compile(
    r"\b(enter now|buy now|place (the |an )?order|execute (the )?entry|go long now|"
    r"entry price is|take the entry)\b",
    re.IGNORECASE,
)

#: Language that treats an old price as upside. Deliberately narrow: discussing a
#: prior high as resistance is not only permitted but wanted, so the pattern
#: matches the *return to it* framing rather than the mention.
_PRIOR_HIGH_AS_UPSIDE = re.compile(
    r"(back (up )?to (its |the )?(previous|prior|former|old) high"
    r"|retrace to (its |the )?(previous|prior|former|old) high"
    r"|recover (to )?(its |the )?(previous|prior|former|old) high"
    r"|return to (its |the )?(previous|prior|former|old) high"
    r"|upside to (its |the )?(previous|prior|former|old) high"
    r"|target(s|ing)? (its |the )?(previous|prior|former|old) high)",
    re.IGNORECASE,
)


#: Public alias. The intraday entry contract has to apply the same rule - a
#: prior high is an obstacle, never upside - while *not* applying the one above
#: it, because an intraday answer is allowed to read as an instruction to enter.
PRIOR_HIGH_AS_UPSIDE = _PRIOR_HIGH_AS_UPSIDE


def reads_as_prior_high_upside(text: str) -> bool:
    """Whether this rationale argues for a rise by pointing at an old price."""

    return bool(_PRIOR_HIGH_AS_UPSIDE.search(text or ""))


@dataclass
class ValidationResult:
    status: ValidationStatus
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status is not ValidationStatus.REJECTED


def validate(
    response: LLMResponse,
    *,
    close: float | None = None,
    threshold_price: float | None = None,
    obstacles=(),
    missing_sections=(),
) -> ValidationResult:
    """Check one Stage 3 response.

    ``missing_sections`` is not an error. A bundle assembled without materials
    produces a different answer from one assembled with an empty materials list,
    and saying so as a warning keeps that visible without pretending the run
    failed.
    """

    result = ValidationResult(status=ValidationStatus.PASSED)
    rationale = response.rationale or ""

    if not rationale.strip():
        result.errors.append("no rationale; a state with no reasoning cannot be reviewed or disputed")

    if _ENTRY_LANGUAGE.search(rationale):
        result.errors.append(
            "the rationale reads as an entry instruction. Stage 3 is an end-of-day analysis with no live "
            "price; entry is a Phase 8 decision and must not be expressed here"
        )

    if _PRIOR_HIGH_AS_UPSIDE.search(rationale):
        result.errors.append(
            "the rationale uses a prior high as upside. A price the security once traded at is where sellers "
            "are waiting (CLAUDE.md 1-11); it belongs in the obstacles, not in the case for a rise"
        )

    # --- the reachable zone ------------------------------------------------
    if response.reachable_zone_high is not None:
        if not response.reachable_zone_basis_kinds:
            result.errors.append("a reachable zone with no basis kind; the zone is a number with no argument behind it")
        else:
            forbidden = [kind for kind in response.reachable_zone_basis_kinds if not isinstance(kind, ZoneBasisKind)]
            if forbidden:
                result.errors.append(f"unrecognised zone basis kinds: {forbidden}")
        if not (response.reachable_zone_basis or "").strip():
            result.errors.append("a reachable zone with no written basis")
        if response.reachable_zone_low is not None and response.reachable_zone_high < response.reachable_zone_low:
            result.errors.append("the reachable zone is inverted")
        if close is not None and response.reachable_zone_high < close:
            result.warnings.append(
                f"the zone's upper bound ({response.reachable_zone_high}) is below the close ({close}); "
                "that is a legitimate thing to conclude and an unusual one to state as a reachable zone"
            )

    # --- threshold and zone must stay distinct -----------------------------
    if threshold_price is not None and response.reachable_zone_high is not None:
        if abs(threshold_price - response.reachable_zone_high) < 1e-9:
            result.errors.append(
                "the 20% threshold and the reachable zone's upper bound are the same number. One has been "
                "derived from the other, which collapses the separation between arithmetic and judgement"
            )

    # --- obstacles ---------------------------------------------------------
    blocking = [
        obstacle
        for obstacle in obstacles
        if response.reachable_zone_high is not None
        and obstacle.get("price_level") is not None
        and close is not None
        and close < obstacle["price_level"] < response.reachable_zone_high
        and not obstacle.get("weakening_evidence")
    ]
    if blocking:
        result.warnings.append(
            f"the zone runs through {len(blocking)} unweakened obstacle(s); permitted, but the rationale "
            "should say why they do not hold"
        )

    if response.state is Stage3State.REJECT and (
        response.reachable_zone_high is not None or response.reachable_zone_low is not None
    ):
        result.repairs.append("dropped the reachable zone from a REJECT: a rejected security has no zone")

    for section in missing_sections:
        result.warnings.append(
            f"section '{section}' was not in the bundle, so the answer was formed without it"
        )

    if result.errors:
        result.status = ValidationStatus.REJECTED
    elif result.repairs:
        result.status = ValidationStatus.REPAIRED

    return result


def apply_repairs(response: LLMResponse, result: ValidationResult) -> LLMResponse:
    """Return a response with the repairs applied.

    Repairs are narrow and must be listed in the result, so a stored output that
    differs from what the model said can be traced to a named rule rather than to
    a quiet rewrite.
    """

    if result.status is not ValidationStatus.REPAIRED:
        return response

    from dataclasses import replace

    if response.state is Stage3State.REJECT:
        return replace(
            response,
            reachable_zone_low=None,
            reachable_zone_high=None,
            reachable_zone_basis_kinds=(),
            reachable_zone_basis=None,
        )
    return response


def twenty_percent_threshold(reference_price: float | None) -> float | None:
    """Arithmetic, and nothing else.

    Kept as a function so no caller is tempted to compute it from a zone or a
    target. The reference price is the entry reference price once one exists;
    before then it is whatever the caller states it is, and the output row
    records which.
    """

    if reference_price is None:
        return None
    return reference_price * 1.20
