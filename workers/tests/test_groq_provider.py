"""The hosted analysis provider, and what it refuses to do to get a request sent.

Three refusals are the substance of this file.

It refuses to send the canonical method to a provider whose terms permit training
on inputs - which is the whole reason Groq was chosen over the free Gemini tier,
encoded as a guard rather than left in a research note.

It refuses to shorten the prompt to fit a free quota. A truncated canonical
prompt would make every answer an answer to a different method, and nothing in
the output would show it; the correct finding is D-32_BLOCKED_FREE_QUOTA.

And it refuses a half-formed structured output. A truncated JSON answer is not a
partial analysis, it is not an analysis.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from surge.analysis.entry_analysis import EntryAnalysisState
from surge.analysis.groq_provider import (
    BLOCKED_FREE_QUOTA,
    ENV_API_KEY,
    GEMINI_FREE_TIER_POLICY,
    GROQ_POLICY,
    CredentialsMissing,
    FreeQuotaExceeded,
    GroqError,
    GroqHostedProvider,
    InputPolicyViolation,
    InputUse,
    Quota,
    Retention,
    StructuredOutputError,
    entry_analysis_schema,
    estimate_tokens,
    preflight,
    response_format,
    stage3_schema,
    strict_models,
)
from surge.analysis.llm import LLMRequest, ProviderKind, Stage3State

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.body = json.dumps(payload).encode()
        self.requested_at = NOW
        self.received_at = NOW

    @property
    def bytes(self):
        return len(self.body)


def _completion(content: dict, *, refusal=None, finish_reason="stop") -> dict:
    message = {"content": json.dumps(content) if content is not None else None}
    if refusal:
        message["refusal"] = refusal
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380},
    }


def _provider(payload, **overrides) -> GroqHostedProvider:
    sent: dict = {}

    def transport(url, **kwargs):
        sent["url"] = url
        sent["headers"] = kwargs.get("headers")
        sent["body"] = json.loads(kwargs["data"].decode())
        sent["method"] = kwargs.get("method")
        return _Response(payload)

    provider = GroqHostedProvider(
        model_id=overrides.pop("model_id", "openai/gpt-oss-120b"),
        api_key="gsk-not-a-real-key",
        transport=transport,
        **overrides,
    )
    provider._sent = sent  # type: ignore[attr-defined]
    return provider


# --------------------------------------------------------------- the policy


def test_groq_is_recorded_as_not_training_on_inputs():
    assert GROQ_POLICY.input_use is InputUse.NOT_USED_FOR_TRAINING
    assert GROQ_POLICY.retention is Retention.NONE_BY_DEFAULT
    assert GROQ_POLICY.zero_data_retention_available
    # Whether it is switched on is a fact about the account, which this code has
    # never seen. Recording False would be a claim; None is the truth.
    assert GROQ_POLICY.zero_data_retention_enabled is None
    assert GROQ_POLICY.may_receive_the_canonical_method


def test_the_free_gemini_tier_is_refused_the_canonical_method():
    """Its own terms say submitted content is used to develop Google products
    and that human reviewers may read it. Every request carries v5.1 in full."""

    assert GEMINI_FREE_TIER_POLICY.input_use is InputUse.USED_FOR_TRAINING
    with pytest.raises(InputPolicyViolation, match="Canonical v5.1 in full"):
        GEMINI_FREE_TIER_POLICY.assert_may_receive_the_canonical_method()


def test_a_provider_whose_terms_are_silent_is_refused_too():
    """NOT_SPECIFIED is not consent, here as everywhere else."""

    from dataclasses import replace

    silent = replace(GROQ_POLICY, input_use=InputUse.NOT_SPECIFIED)

    with pytest.raises(InputPolicyViolation):
        silent.assert_may_receive_the_canonical_method()


def test_the_policy_is_enforced_on_every_call_not_just_at_construction():
    from dataclasses import replace

    provider = _provider(_completion({"state": "REJECT", "rationale": "no"}))
    provider.policy = replace(GROQ_POLICY, input_use=InputUse.USED_FOR_TRAINING)

    with pytest.raises(InputPolicyViolation):
        provider.analyse(LLMRequest(prompt="anything", bundle=None))


# ------------------------------------------------------------ the preflight


def test_japanese_text_is_not_estimated_at_a_quarter_of_its_size():
    """The canonical prompt is Japanese. A characters-over-four rule would say a
    prompt fits when it does not."""

    japanese = "短期急騰の判断基準について" * 100
    latin = "a decision rule about short term surges " * 100

    assert estimate_tokens(japanese) > len(japanese) * 0.9
    assert estimate_tokens(latin) < len(latin) * 0.35


def test_an_unmeasured_quota_does_not_pass_the_preflight():
    """Groq's free limits differ per model and per account. A preflight run
    against a number from a documentation page passes until it does not."""

    check = preflight("some prompt", quota=Quota())

    assert not check.fits
    assert "have not been measured" in check.reason
    assert check.decision_id is None


def test_a_prompt_over_the_per_request_limit_is_the_blocked_quota_decision():
    check = preflight("x" * 400_000, quota=Quota(max_input_tokens_per_request=8_000))

    assert not check.fits
    assert check.decision_id == BLOCKED_FREE_QUOTA
    assert "not shortened to fit a quota" in check.reason


def test_the_preflight_has_no_way_to_shorten_anything():
    """Structural, not a convention: there is no truncation argument to reach
    for when a bundle does not fit."""

    import inspect

    parameters = set(inspect.signature(preflight).parameters)

    assert parameters == {"prompt", "quota", "requests_per_day"}


def test_a_batch_that_needs_pacing_still_fits():
    check = preflight("x" * 3_500, quota=Quota(max_tokens_per_minute=30_000), requests_per_day=100)

    assert check.fits
    assert "has to be paced" in check.reason


def test_a_call_over_quota_raises_rather_than_sending():
    provider = _provider(
        _completion({"state": "REJECT", "rationale": "no"}),
        quota=Quota(max_input_tokens_per_request=10),
    )

    with pytest.raises(FreeQuotaExceeded, match=BLOCKED_FREE_QUOTA):
        provider.analyse(LLMRequest(prompt="x" * 10_000, bundle=None))


# ------------------------------------------------------ structured outputs


def test_the_stage3_schema_is_strict_shaped():
    schema = stage3_schema()

    assert schema["additionalProperties"] is False
    # Strict mode requires every property in `required`; optional values are
    # expressed as a union with null instead.
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["reachable_zone_high"]["type"] == ["number", "null"]
    assert schema["properties"]["state"]["enum"] == [s.value for s in Stage3State]


def test_the_entry_schema_carries_the_two_fields_an_entry_cannot_do_without():
    schema = entry_analysis_schema()

    assert "proposed_initial_failure_line" in schema["properties"]
    assert "decision_price_used" in schema["properties"]
    assert schema["properties"]["state"]["enum"] == [s.value for s in EntryAnalysisState]
    assert set(schema["required"]) == set(schema["properties"])


def test_strict_is_requested_only_for_a_model_documented_to_support_it():
    documented = _provider(_completion({"state": "REJECT", "rationale": "no"}))
    undocumented = _provider(
        _completion({"state": "REJECT", "rationale": "no"}), model_id="some/new-model"
    )

    assert documented.supports_strict
    assert not undocumented.supports_strict

    documented.analyse(LLMRequest(prompt="p", bundle=None))
    undocumented.analyse(LLMRequest(prompt="p", bundle=None))

    assert documented._sent["body"]["response_format"]["json_schema"]["strict"] is True
    assert undocumented._sent["body"]["response_format"]["json_schema"]["strict"] is False


def test_the_strict_model_list_is_configuration():
    """Groq's catalogue turns over faster than this repository does."""

    assert strict_models({"GROQ_STRICT_MODELS": "a/one, b/two"}) == frozenset({"a/one", "b/two"})


def test_the_response_format_is_the_documented_shape():
    formatted = response_format("entry_analysis", {"type": "object"}, strict=True)

    assert formatted["type"] == "json_schema"
    assert formatted["json_schema"]["name"] == "entry_analysis"
    assert formatted["json_schema"]["strict"] is True


# ------------------------------------------------------------- the answers


def test_a_stage3_answer_becomes_an_llm_response():
    provider = _provider(
        _completion(
            {
                "state": "WATCH_BREAKOUT",
                "rationale": "the level has not been cleared on volume yet",
                "confidence_note": None,
                "reachable_zone_low": 1010.0,
                "reachable_zone_high": 1150.0,
                "reachable_zone_basis_kinds": ["VOLUME_STRUCTURE"],
                "reachable_zone_basis": "three sessions of expanding volume under the level",
                "concepts_considered": ["VOLUME_DRY_UP"],
            }
        )
    )

    answer = provider.analyse(LLMRequest(prompt="p", bundle=None))

    assert answer.state is Stage3State.WATCH_BREAKOUT
    assert answer.provider_kind is ProviderKind.HOSTED_LLM
    assert answer.model_id == "openai/gpt-oss-120b"
    assert answer.reachable_zone_high == 1150.0
    assert provider.last_usage.prompt_tokens == 1200


def test_an_entry_answer_becomes_an_entry_analysis_response():
    provider = _provider(
        _completion(
            {
                "state": "ENTRY",
                "rationale": "cleared the level on expanding volume; enter now",
                "decision_price_used": 1000,
                "proposed_initial_failure_line": 940,
                "reachable_zone_low": 1010,
                "reachable_zone_high": 1150,
                "reachable_zone_basis_kinds": ["VOLUME_STRUCTURE"],
                "reachable_zone_basis": "expanding volume through the level",
                "concepts_considered": [],
                "watch_trigger_description": None,
                "reject_reason": None,
            }
        )
    )

    answer = provider.analyse_entry(LLMRequest(prompt="p", bundle=None))

    assert answer.state is EntryAnalysisState.ENTRY
    # Decimal, not float. The failure line is compared with prices and stored at
    # a fixed scale; a float here would put a rounding error in an audit trail.
    assert answer.proposed_initial_failure_line == Decimal("940")
    assert isinstance(answer.decision_price_used, Decimal)
    assert not answer.is_a_stand_in


def test_a_refusal_is_not_an_answer():
    provider = _provider(_completion(None, refusal="I can't help with that"))

    with pytest.raises(StructuredOutputError, match="refused"):
        provider.analyse_entry(LLMRequest(prompt="p", bundle=None))


def test_a_truncated_answer_is_not_a_partial_answer():
    provider = _provider(_completion(None, finish_reason="length"))

    with pytest.raises(StructuredOutputError, match="not an answer"):
        provider.analyse(LLMRequest(prompt="p", bundle=None))


def test_content_that_is_not_json_is_refused():
    payload = {
        "choices": [{"message": {"content": "sorry, here is some prose"}, "finish_reason": "stop"}]
    }
    provider = _provider(payload)

    with pytest.raises(StructuredOutputError, match="not JSON"):
        provider.analyse(LLMRequest(prompt="p", bundle=None))


# ------------------------------------------------------------ credentials


def test_a_missing_key_names_the_file_and_the_consequence():
    with pytest.raises(CredentialsMissing, match=r"\.env\.local"):
        GroqHostedProvider.from_env({ENV_API_KEY: ""})


def test_a_missing_model_is_refused_rather_than_defaulted():
    """Pinning a model in code would make the output row's model_id a fiction
    the first time the catalogue changed."""

    with pytest.raises(GroqError, match="configuration, not code"):
        GroqHostedProvider.from_env({ENV_API_KEY: "gsk-x"})


def test_the_key_never_renders_itself():
    provider = GroqHostedProvider(model_id="openai/gpt-oss-20b", api_key="gsk-secret")

    assert "gsk-secret" not in repr(provider)


def test_the_registry_row_records_the_policy_and_costs_nothing():
    provider = GroqHostedProvider(model_id="openai/gpt-oss-20b", api_key="x")

    row = provider.registry_row()

    assert row["provider_kind"] == "HOSTED_LLM"
    assert row["monthly_cost_jpy"] == 0
    # Not live-verified until a real call has been made and seen to work.
    assert row["live_verified_at"] is None
    assert row["enabled"] is False
    assert "NOT_USED_FOR_TRAINING" in row["notes"]
    assert "Zero Data Retention" in row["notes"]
