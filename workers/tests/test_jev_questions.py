"""The prediction contract as typed Jev questions: shape and completeness.

No request is made; these pin that the questions are valid for the documented
API (https://docs.typesafe.ai/api), that they map onto the current contract,
and that an incomplete or malformed answer is caught rather than trusted.
"""

from __future__ import annotations

from surge.analysis.entry_analysis import EntryAnalysisState
from surge.analysis.jev_questions import BASIS, UPSIDE_BANDS, completeness, questions
from surge.analysis.llm import ZoneBasisKind


def test_the_decision_keys_are_the_current_contracts_states():
    assert set(questions()["decision"]["criteria"]) == {state.value for state in EntryAnalysisState}


def test_every_zone_basis_kind_is_its_own_yes_no_question():
    asked = questions()
    assert set(BASIS) == set(ZoneBasisKind)
    for kind in ZoneBasisKind:
        assert asked[f"basis_{kind.value.lower()}"]["type"] == "noul"


def test_the_shapes_are_valid_for_the_api():
    for key, question in questions().items():
        assert question["type"] in {"noul", "choice", "score"}, key
        assert question["instructions"], key
        if question["type"] == "choice":
            assert 1 <= len(question["criteria"]) <= 255
        if question["type"] == "score":
            assert 2 <= len(question["criteria"]) <= 10
        if question["type"] == "noul":
            assert set(question["criteria"]) == {"true", "false"}


def test_nothing_code_decides_is_asked():
    """The target, the filter, prices and dates are code's; no question asks for a number."""

    text = " ".join(q["instructions"] for q in questions().values()).lower()
    for forbidden in ("what price", "which price", "how many yen", "what date", "calculate"):
        assert forbidden not in text


def _complete_response():
    answers = {
        "decision": {"type": "choice", "choice": "REJECT",
                     "probabilities": {"ENTRY": 0.1, "WATCH_BREAKOUT": 0.1, "WATCH_PULLBACK": 0.1,
                                       "WATCH_OTHER": 0.1, "REJECT": 0.6}, "confidence": 0.5},
        "reaches_target": {"type": "noul", "noul": 0.12},
        "upside_band": {"type": "score", "score": 1.2,
                        "probabilities": {str(i): 1 / len(UPSIDE_BANDS) for i in range(len(UPSIDE_BANDS))},
                        "confidence": 0.3},
    }
    for kind in ZoneBasisKind:
        answers[f"basis_{kind.value.lower()}"] = {"type": "noul", "noul": 0.3}
    return {"model": "jev-latest", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_a_complete_response_has_no_problems():
    assert completeness(questions(), _complete_response()) == []


def test_the_gateway_shape_differs_only_by_the_yes_no_renames():
    from surge.analysis.jev_questions import from_gateway, to_gateway

    asked = questions()
    gateway = to_gateway(asked)
    assert {q["type"] for q in gateway.values()} == {"boolean", "choice", "score"}
    assert gateway["decision"] == asked["decision"] and gateway["upside_band"] == asked["upside_band"]
    assert gateway["reaches_target"]["criteria"] == asked["reaches_target"]["criteria"]

    answers = {key: {"type": "boolean", "probability": 0.4} for key, q in gateway.items() if q["type"] == "boolean"}
    answers["decision"] = {"type": "choice", "choice": "REJECT",
                           "probabilities": {"ENTRY": 0.1, "WATCH_BREAKOUT": 0.1, "WATCH_PULLBACK": 0.1,
                                             "WATCH_OTHER": 0.1, "REJECT": 0.6}}
    answers["upside_band"] = {"type": "score", "score": 1.0,
                              "probabilities": {str(i): 0.2 for i in range(len(UPSIDE_BANDS))}}
    response = {"answers": from_gateway(answers)}

    assert completeness(asked, response, require_confidence=False) == []
    assert completeness(asked, response)  # TypeSafe's own API does give confidence


def test_missing_wrong_type_and_malformed_answers_are_caught():
    response = _complete_response()
    del response["answers"]["reaches_target"]
    response["answers"]["upside_band"]["type"] = "noul"
    response["answers"]["decision"]["probabilities"]["REJECT"] = 0.9
    response["answers"]["basis_sector_move"]["noul"] = 1.5
    response["answers"]["unasked"] = {"type": "noul", "noul": 0.5}

    problems = completeness(questions(), response)

    assert any(p.startswith("reaches_target: no answer") for p in problems)
    assert any(p.startswith("upside_band: answered as") for p in problems)
    assert any(p.startswith("decision: probabilities sum") for p in problems)
    assert any(p.startswith("basis_sector_move: noul") for p in problems)
    assert any("unasked" in p for p in problems)
