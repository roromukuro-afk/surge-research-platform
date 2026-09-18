"""The prediction contract as typed Jev questions (a fit smoke, not the pipeline).

TypeSafe's Jev returns typed decisions - Choice, Score, Noul (the probability
of yes) - instead of text (https://docs.typesafe.ai/api). This maps the current
prediction-producing contract (``entry_analysis_schema()``) onto those types.

Three rules decide the mapping:

* **What code decides is not asked.** The S0 close, the target (close x 1.20),
  the 3,000 yen filter, the horizon and the checkpoints are computed by code
  (eod-prediction-1.0.0) and given to the model as facts in the state.
* **Numbers are not asked for.** TypeSafe's own notes for jev-1.13 say it
  "struggles with tasks that require numeric precision" and to keep arithmetic
  in code. So the failure line and the reachable-zone bounds are not Jev's;
  the upside is asked as ordered bands, never as a price.
* **Text is not asked for.** Rationale, zone basis prose, watch trigger prose
  and reject reason prose have no Jev type; they are dropped, not faked.

Mapping of the current schema:

========================== ============ ============================================
current field              Jev          note
========================== ============ ============================================
state (5 values)           choice       ``decision``; same five keys
reachable_zone_basis_kinds 6 x noul     one independent yes/no per kind
reachable_zone_low/high    score        ``upside_band``: 5 ordered bands, no prices
decision_price_used        code         the S0 confirmed close
proposed_initial_failure_line  not Jev  a price; code or a generative model
rationale / *_basis / *_description / reject_reason / concepts_considered  dropped (text)
(new) reaches_target       noul         P(target reached S1..T+20); not displayed (CLAUDE.md 1-17)
========================== ============ ============================================
"""

from __future__ import annotations

from surge.analysis.llm import ZoneBasisKind

MODEL = "jev-latest"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
#: TypeSafe's published price, per million input tokens; output is not charged.
USD_PER_MILLION_INPUT_TOKENS = 0.042

DECISIONS = {
    "ENTRY": (
        "Predict now: following the method, the setup, the materials, supply and demand and "
        "volume support a rise from the S0 confirmed close to the target within the horizon."
    ),
    "WATCH_BREAKOUT": "Not yet: worth predicting only after a breakout above a nearby resistance.",
    "WATCH_PULLBACK": "Not yet: worth predicting only after a pullback to a nearby support.",
    "WATCH_OTHER": "Not yet: worth watching for some other, specific condition.",
    "REJECT": "No prediction: the method does not support the rise, or one of its conditions fails.",
}

BASIS = {
    ZoneBasisKind.CURRENT_MATERIAL: "a current catalyst or material that the price has not yet absorbed",
    ZoneBasisKind.SUPPLY_STRUCTURE: "a supply and demand structure that favours a rise",
    ZoneBasisKind.VOLUME_STRUCTURE: "volume behaviour that supports a rise",
    ZoneBasisKind.SUPPORT_RESISTANCE: "support and resistance levels that leave room up to the target",
    ZoneBasisKind.VOLATILITY_RANGE: "the stock's usual range, which makes a move of this size plausible",
    ZoneBasisKind.SECTOR_MOVE: "a sector or theme move that carries the stock",
}

UPSIDE_BANDS = [
    "No realistic upside",
    "Up to +10% above the S0 close",
    "+10% to +20% above the S0 close",
    "+20% to +30% above the S0 close",
    "More than +30% above the S0 close",
]


def questions() -> dict:
    """The typed questions, keyed as the answers come back."""

    asked = {
        "decision": {
            "type": "choice",
            "instructions": (
                "Following the method in the state (Canonical v5.1 and the later addenda), what is "
                "the decision for this security as of the S0 confirmed close?"
            ),
            "criteria": dict(DECISIONS),
        },
        "reaches_target": {
            "type": "noul",
            "instructions": (
                "Following the method, will the price reach the target price given in the state "
                "(the S0 confirmed close x 1.20) at some point from S1 through T+20?"
            ),
            "criteria": {
                "true": "It reaches the target within the horizon",
                "false": "It does not reach the target within the horizon",
            },
        },
        "upside_band": {
            "type": "score",
            "instructions": (
                "Following the method's reachable-zone reasoning (a prior high is an obstacle, never "
                "a reason to expect a rise), how far can the price realistically rise from the S0 "
                "confirmed close within the horizon?"
            ),
            "criteria": list(UPSIDE_BANDS),
        },
    }
    for kind, description in BASIS.items():
        asked[f"basis_{kind.value.lower()}"] = {
            "type": "noul",
            "instructions": f"Is {description} a basis for this security's upside now?",
            "criteria": {"true": f"Yes: {description}", "false": "No"},
        }
    return asked


#: Vercel AI Gateway serves Jev as ``typesafe-ai/jev`` through the AI SDK's
#: ``experimental_evaluate`` (https://vercel.com/docs/ai-gateway/modalities/evaluation),
#: evaluation being AI SDK only. The shapes are TypeSafe's with two renames - the
#: yes/no type is ``boolean`` and its answer field is ``probability`` - and the
#: choice and score confidence arrives beside the answers, in
#: ``providerMetadata.typesafe.confidence`` (measured 2026-09-18).
GATEWAY_MODEL = "typesafe-ai/jev"


def to_gateway(asked: dict) -> dict:
    """TypeSafe-typed questions as the Gateway takes them."""

    converted = {}
    for key, question in asked.items():
        question = dict(question)
        if question["type"] == "noul":
            question["type"] = "boolean"
        converted[key] = question
    return converted


def from_gateway(answers: dict, confidence: dict | None = None) -> dict:
    """Gateway answers back in TypeSafe's shape, so one check reads both.

    ``confidence`` is ``providerMetadata.typesafe.confidence``; each value is put
    back on its choice or score answer, where TypeSafe's own API returns it.
    """

    converted = {}
    for key, answer in (answers or {}).items():
        answer = dict(answer)
        if answer.get("type") == "boolean":
            answer = {"type": "noul", "noul": answer.get("probability")}
        elif confidence and key in confidence and "confidence" not in answer:
            answer["confidence"] = confidence[key]
        converted[key] = answer
    return converted


def completeness(asked: dict, response: dict, *, require_confidence: bool = True) -> list[str]:
    """What is wrong with a response against the questions asked. Empty means complete.

    ``require_confidence`` is False for answers that came through the Gateway,
    whose documented choice and score answers carry probabilities but no
    confidence; its absence there is the shape, not a defect.
    """

    problems = []
    answers = response.get("answers") or {}
    for key, question in asked.items():
        answer = answers.get(key)
        if not isinstance(answer, dict):
            problems.append(f"{key}: no answer")
            continue
        kind = question["type"]
        if answer.get("type") != kind:
            problems.append(f"{key}: answered as {answer.get('type')!r}, asked as {kind!r}")
            continue
        if kind == "noul":
            value = answer.get("noul")
            if not isinstance(value, int | float) or not 0 <= value <= 1:
                problems.append(f"{key}: noul {value!r} is not a probability")
            continue
        probabilities = answer.get("probabilities") or {}
        expected = set(question["criteria"]) if kind == "choice" else {str(i) for i in range(len(question["criteria"]))}
        if set(probabilities) != expected:
            problems.append(f"{key}: probabilities cover {sorted(probabilities)}, expected {sorted(expected)}")
        elif abs(sum(probabilities.values()) - 1.0) > 0.01:
            problems.append(f"{key}: probabilities sum to {sum(probabilities.values()):.4f}")
        confidence = answer.get("confidence")
        if (require_confidence or confidence is not None) and (
            not isinstance(confidence, int | float) or not 0 <= confidence <= 1
        ):
            problems.append(f"{key}: confidence {confidence!r} is not in [0, 1]")
        if kind == "choice" and answer.get("choice") not in question["criteria"]:
            problems.append(f"{key}: choice {answer.get('choice')!r} is not an option")
        if kind == "score" and not isinstance(answer.get("score"), int | float):
            problems.append(f"{key}: score {answer.get('score')!r} is not a number")
    extra = set(answers) - set(asked)
    if extra:
        problems.append(f"answers nobody asked for: {sorted(extra)}")
    return problems


__all__ = ["BASIS", "DECISIONS", "ENDPOINT", "GATEWAY_MODEL", "MODEL", "UPSIDE_BANDS",
           "USD_PER_MILLION_INPUT_TOKENS", "completeness", "from_gateway", "questions", "to_gateway"]
