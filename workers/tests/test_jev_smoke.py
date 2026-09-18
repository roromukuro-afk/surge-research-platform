"""What the Jev smoke keeps from one Gateway evaluation (no request is made here).

The result below has the shape measured through Vercel AI Gateway on
2026-09-18 (typesafe-ai/jev), with synthetic values.
"""

from __future__ import annotations

import json

from surge.analysis.jev_questions import UPSIDE_BANDS, questions, to_gateway
from surge.jobs.jev_smoke import gateway_record


def _gateway_result():
    asked = to_gateway(questions())
    answers = {key: {"type": "boolean", "probability": 0.3} for key, q in asked.items() if q["type"] == "boolean"}
    answers["decision"] = {"type": "choice", "choice": "REJECT",
                           "probabilities": {"ENTRY": 0.1, "WATCH_BREAKOUT": 0.05, "WATCH_PULLBACK": 0.05,
                                             "WATCH_OTHER": 0.1, "REJECT": 0.7}}
    answers["upside_band"] = {"type": "score", "score": 1.5,
                              "probabilities": {str(i): 0.2 for i in range(len(UPSIDE_BANDS))}}
    return {
        "latencyMs": 1021,
        "answers": answers,
        "usage": {"inputTokens": 21858, "outputTokens": 211, "totalTokens": 22069},
        "providerMetadata": {
            "typesafe": {"confidence": {"decision": 0.69, "upside_band": 0.4}},
            "gateway": {
                "routing": {"finalProvider": "typesafe-ai", "modelAttempts": [{"providerAttempts": [
                    {"provider": "typesafe-ai", "success": True, "startTime": 1000, "endTime": 1297}]}]},
                "cost": "0.000918036",
                "generationId": "gen_TEST",
            },
        },
        "response": {"modelId": "typesafe-ai/jev"},
    }


def test_a_gateway_answer_is_kept_in_typesafes_shape_with_its_confidence():
    record = gateway_record(_gateway_result(), asked=questions(), label="run1", request_sha256="0" * 64)

    assert record["status"] == "ok"
    assert record["completeness_problems"] == []
    assert record["answers"]["reaches_target"] == {"type": "noul", "noul": 0.3}
    assert record["answers"]["decision"]["confidence"] == 0.69
    assert record["answers"]["upside_band"]["confidence"] == 0.4
    assert record["gateway_cost_usd"] == "0.000918036"
    assert record["provider_attempt_seconds"] == 0.297
    assert record["resolved_provider"] == "typesafe-ai"
    assert record["usage"]["inputTokens"] == 21858


def test_the_record_holds_no_request():
    """The request carries the state, and so the canonical text; none of it is kept."""

    record = gateway_record(_gateway_result(), asked=questions(), label="run1", request_sha256="0" * 64)
    text = json.dumps(record)

    assert "state" not in record
    assert "canonical" not in text.lower()


def test_an_error_is_recorded_as_one_and_answers_nothing():
    record = gateway_record({"latencyMs": 50, "error": {"name": "GatewayError", "message": "x"}},
                           asked=questions(), label="run1", request_sha256="0" * 64)

    assert record["status"] == "error"
    assert record["completeness_problems"] == ["no answers"]
