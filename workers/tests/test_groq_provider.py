"""The hosted analysis provider, and what it refuses to do to get a request sent.

Three refusals are the substance of this file.

It refuses to send the canonical method to a provider whose terms permit training
on inputs - which is the whole reason Groq was chosen over the free Gemini tier,
encoded as a guard rather than left in a research note.

It refuses to shorten the prompt to fit a free quota. A truncated canonical
prompt would make every answer an answer to a different method, and nothing in
the output would show it; the correct finding is ANALYSIS_FREE_QUOTA_BLOCKED.

And it refuses a half-formed structured output. A truncated JSON answer is not a
partial analysis, it is not an analysis.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import (
    UTC,
    date,  # noqa: E402
    datetime,
)
from decimal import Decimal

import pytest

from surge.analysis.entry_analysis import EntryAnalysisState
from surge.analysis.execution import FailureClass, classify_provider_failure  # noqa: E402
from surge.analysis.groq_provider import (  # noqa: E402
    ANALYSIS_EXTERNAL_TOOL_USED,
    ANALYSIS_FREE_QUOTA_BLOCKED,
    DEFAULT_COMPLETION_RESERVE,
    ENV_API_KEY,
    GEMINI_FREE_TIER_POLICY,
    GROQ_POLICY,
    JSON_SCHEMA_UNSUPPORTED_BY_CURRENT_DOCS,
    PUBLISHED_FREE_LIMITS_GPT_OSS,
    CredentialsMissing,
    ExternalToolUsed,
    FreeQuotaExceeded,
    GroqError,
    GroqHostedProvider,
    InputPolicyViolation,
    InputUse,
    OutputMode,
    Quota,
    QuotaUnknown,
    RateLimited,
    RequestMode,
    RequestTooLarge,
    Retention,
    StructuredOutputError,
    entry_analysis_schema,
    estimate_tokens,
    failure_for_status,
    policy_from_env,
    preflight,
    quota_from_headers,
    response_format,
    schema_violations,
    stage3_schema,
    strict_models,
    uses_built_in_tools,
)
from surge.analysis.llm import LLMRequest, ProviderKind, Stage3State

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


class _Response:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self.body = json.dumps(payload).encode()
        self.requested_at = NOW
        self.received_at = NOW
        self.headers = headers or {}

    @property
    def bytes(self):
        return len(self.body)


#: A quota that has been measured. Every call that is meant to succeed needs
#: one, because an unmeasured quota now refuses to send.
MEASURED = Quota(
    max_tokens_per_minute=30_000,
    requests_per_day=1_000,
    source="fixture: pretend these came from response headers",
)


def _completion(content: dict, *, refusal=None, finish_reason="stop") -> dict:
    message = {"content": json.dumps(content) if content is not None else None}
    if refusal:
        message["refusal"] = refusal
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380},
    }


#: The same terms Groq publishes, read against an account where ZDR has been
#: switched on and confirmed. Nothing in the repository asserts that about the
#: real account, because nobody has looked at the real account.
ZDR_CONFIRMED = replace(GROQ_POLICY, zero_data_retention_enabled=True)


def _provider(payload, **overrides) -> GroqHostedProvider:
    sent: dict = {}

    def transport(url, **kwargs):
        sent["url"] = url
        sent["headers"] = kwargs.get("headers")
        sent["body"] = json.loads(kwargs["data"].decode())
        sent["method"] = kwargs.get("method")
        return _Response(payload)

    overrides.setdefault("quota", MEASURED)
    provider = GroqHostedProvider(
        model_id=overrides.pop("model_id", "openai/gpt-oss-120b"),
        api_key="gsk-not-a-real-key",
        transport=transport,
        # An account whose Zero Data Retention has been confirmed on. The real
        # policy records it as unconfirmed and the production gate refuses it -
        # which is asserted on its own below. Tests about the request and the
        # response shape have to get past the gate to reach what they are
        # testing, so they say explicitly which account they are pretending to
        # be rather than the gate being lenient by default.
        policy=overrides.pop("policy", ZDR_CONFIRMED),
        **overrides,
    )
    provider._sent = sent  # type: ignore[attr-defined]
    return provider


# --------------------------------------------------------------- the policy


def test_groq_is_recorded_as_not_training_on_inputs():
    assert GROQ_POLICY.input_use is InputUse.NOT_USED_FOR_TRAINING
    assert GROQ_POLICY.zero_data_retention_available
    # Whether it is switched on is a fact about the account, which this code has
    # never seen. Recording False would be a claim; None is the truth.
    assert GROQ_POLICY.zero_data_retention_enabled is None
    assert GROQ_POLICY.may_receive_the_canonical_method


def test_retention_is_the_weaker_of_the_two_things_the_terms_say():
    """"Groq does not retain customer data for inference requests" sits beside a
    documented possibility of transient retention for reliability and abuse
    handling. Until ZDR is confirmed on, the weaker one is what is true."""

    assert GROQ_POLICY.retention is Retention.TRANSIENT_FOR_ABUSE_AND_RELIABILITY


def test_the_privacy_gate_needs_zdr_confirmed_on_the_account():
    """Not training on inputs makes an experiment acceptable. Routine production
    traffic needs ZDR actually switched on, and "available" is a fact about the
    product rather than about the account."""

    from dataclasses import replace

    assert not GROQ_POLICY.privacy_gate_passes
    assert "never seen the account" in GROQ_POLICY.privacy_gate_detail

    confirmed = replace(GROQ_POLICY, zero_data_retention_enabled=True)
    assert confirmed.privacy_gate_passes

    off = replace(GROQ_POLICY, zero_data_retention_enabled=False)
    assert not off.privacy_gate_passes
    assert "switched off" in off.privacy_gate_detail


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

    provider = _provider(_completion(_full_stage3()))
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
    assert check.reason_code is None


def test_a_prompt_over_the_per_request_limit_is_the_blocked_quota_decision():
    check = preflight("x" * 400_000, quota=Quota(max_input_tokens_per_request=8_000))

    assert not check.fits
    assert check.reason_code == ANALYSIS_FREE_QUOTA_BLOCKED
    assert "not shortened to fit a quota" in check.reason


def test_the_preflight_has_no_way_to_shorten_anything():
    """Structural, not a convention: there is no truncation argument to reach
    for when a bundle does not fit. The reserve sizes the *completion*, which
    makes a request larger rather than smaller."""

    import inspect

    parameters = set(inspect.signature(preflight).parameters)

    assert parameters == {"prompt", "quota", "requests_per_day", "reserved_output_tokens"}
    assert not {p for p in parameters if "max" in p or "trunc" in p or "chars" in p}


def test_a_combined_meter_counts_the_completion_reserve_too():
    """Groq's free tier meters one figure, so the model's own output is spent
    from the same allowance as the prompt. Weighing only the input approves
    requests the API then refuses."""

    prompt = "x" * 14_000  # about 4,000 estimated tokens

    without_reserve = preflight(prompt, quota=Quota(max_tokens_per_minute=5_000),
                                reserved_output_tokens=0)
    with_reserve = preflight(prompt, quota=Quota(max_tokens_per_minute=5_000),
                             reserved_output_tokens=2_048)

    assert without_reserve.fits
    assert not with_reserve.fits
    assert with_reserve.reason_code == ANALYSIS_FREE_QUOTA_BLOCKED
    assert "completion reserve" in with_reserve.reason


def test_only_a_prompt_that_is_over_by_itself_is_called_unsendable():
    """Measured (D-256): Groq counts the completion as less than
    max_completion_tokens. So "cannot be sent at all" is certain only when the
    prompt alone is over; when it is the reserve that tips it over, Groq may
    well accept - it still does not fit a paced batch, and the reason says the
    contract smoke decides rather than claiming a refusal nobody observed."""

    prompt = "x" * 14_000  # about 4,000 estimated tokens

    prompt_over = preflight(prompt, quota=Quota(max_tokens_per_minute=3_000),
                            reserved_output_tokens=2_048)
    reserve_over = preflight(prompt, quota=Quota(max_tokens_per_minute=5_000),
                             reserved_output_tokens=2_048)

    assert not prompt_over.fits and not reserve_over.fits
    assert "cannot be sent at all" in prompt_over.reason
    assert "cannot be sent at all" not in reserve_over.reason
    assert "contract smoke decides" in reserve_over.reason


def test_separate_input_and_output_meters_are_judged_separately():
    """An account with ITPM and OTPM is not the same as one combined figure,
    and adding them would refuse requests that fit."""

    prompt = "x" * 14_000

    ok = preflight(
        prompt,
        quota=Quota(max_input_tokens_per_minute=5_000, max_output_tokens_per_minute=3_000),
        reserved_output_tokens=2_048,
    )
    too_much_output = preflight(
        prompt,
        quota=Quota(max_input_tokens_per_minute=5_000, max_output_tokens_per_minute=1_000),
        reserved_output_tokens=2_048,
    )

    assert ok.fits
    assert "separately measured" in ok.reason
    assert not too_much_output.fits
    assert "output allowance" in too_much_output.reason


def test_the_default_reserve_is_the_one_the_provider_actually_asks_for():
    provider = GroqHostedProvider(model_id="openai/gpt-oss-20b", api_key="x")

    assert DEFAULT_COMPLETION_RESERVE == provider.max_completion_tokens


def test_a_batch_that_needs_pacing_still_fits():
    check = preflight("x" * 3_500, quota=Quota(max_tokens_per_minute=30_000), requests_per_day=100)

    assert check.fits
    assert "has to be paced" in check.reason


def test_an_unmeasured_quota_refuses_to_send_rather_than_sending_anyway():
    """The hole this closes: fits=False with no reason code fell through the
    guard and the canonical method went out before anyone knew it could."""

    sent = []

    def transport(url, **kwargs):
        sent.append(url)
        return _Response(_completion(_full_stage3()))

    provider = GroqHostedProvider(
        model_id="openai/gpt-oss-120b",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=Quota(),
    )

    with pytest.raises(QuotaUnknown, match="quota_probe"):
        provider.analyse(LLMRequest(prompt="the canonical method", bundle=None))

    assert sent == []


def test_the_probe_sends_no_response_format_at_all():
    """It exists to read headers. Asking for structured output would make it
    depend on the very capability it is partly being run to discover."""

    sent = {}

    def transport(url, **kwargs):
        sent["body"] = json.loads(kwargs["data"].decode())
        return _Response(_completion({"ok": True}),
                         headers={"x-ratelimit-limit-tokens": "8000"})

    provider = GroqHostedProvider(
        model_id="groq/compound",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=Quota(),
    )
    provider.quota_probe()

    assert "response_format" not in sent["body"]


def test_the_probe_carries_no_canonical_prompt_and_no_market_data():
    """A probe that carried the method would defeat its own purpose."""

    sent = {}

    def transport(url, **kwargs):
        sent["body"] = json.loads(kwargs["data"].decode())
        return _Response(
            _completion({"ok": True}),
            headers={
                "x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-limit-requests": "1000",
            },
        )

    provider = GroqHostedProvider(
        model_id="openai/gpt-oss-120b",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=Quota(),
    )

    measured = provider.quota_probe()

    assert measured.max_tokens_per_minute == 8_000
    assert measured.requests_per_day == 1_000
    assert measured.is_known
    assert provider.quota is measured
    content = sent["body"]["messages"][0]["content"]
    assert len(content) < 60
    assert "canonical" not in content.lower()
    assert "response_format" not in sent["body"]


def test_a_probe_that_learns_nothing_says_so():
    def transport(url, **kwargs):
        return _Response(_completion({"ok": True}), headers={})

    provider = GroqHostedProvider(
        model_id="openai/gpt-oss-120b",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=Quota(),
    )

    with pytest.raises(QuotaUnknown, match="no rate-limit headers"):
        provider.quota_probe()


def test_the_documented_header_asymmetry_is_read_correctly():
    """x-ratelimit-limit-requests is per DAY and x-ratelimit-limit-tokens is per
    MINUTE. Reading either as the other is wrong by orders of magnitude."""

    quota = quota_from_headers(
        {"X-RateLimit-Limit-Tokens": "8000", "X-RateLimit-Limit-Requests": "1000"},
        source="test",
    )

    assert quota.max_tokens_per_minute == 8_000
    assert quota.requests_per_day == 1_000


def test_the_published_free_limits_are_recorded_for_comparison():
    """Used to decide whether an account is worth opening. Not used to send:
    the account's own headers replace them first."""

    assert PUBLISHED_FREE_LIMITS_GPT_OSS["tokens_per_minute"] == 8_000
    assert PUBLISHED_FREE_LIMITS_GPT_OSS["requests_per_day"] == 1_000


def test_a_call_over_quota_raises_rather_than_sending():
    provider = _provider(
        _completion(_full_stage3()),
        quota=Quota(max_input_tokens_per_request=10),
    )

    with pytest.raises(FreeQuotaExceeded, match=ANALYSIS_FREE_QUOTA_BLOCKED):
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


def _full_entry(**overrides) -> dict:
    """A complete, contract-valid Entry answer."""

    base = {
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
    base.update(overrides)
    return base


def _full_stage3(**overrides) -> dict:
    base = {
        "state": "REJECT",
        "rationale": "no route fired",
        "confidence_note": None,
        "reachable_zone_low": None,
        "reachable_zone_high": None,
        "reachable_zone_basis_kinds": [],
        "reachable_zone_basis": None,
        "concepts_considered": [],
    }
    base.update(overrides)
    return base


def test_a_documented_model_gets_a_strict_schema():
    documented = _provider(_completion(_full_stage3()))

    assert documented.output_mode is OutputMode.STRICT_JSON_SCHEMA
    documented.analyse(LLMRequest(prompt="p", bundle=None))

    assert documented._sent["body"]["response_format"]["json_schema"]["strict"] is True


def test_an_undocumented_model_gets_json_object_rather_than_a_schema():
    """Groq's structured-outputs page is a positive list. A family missing from
    it is a documented absence, so the safe reading is json_object plus a schema
    check on this side - not a best-effort schema nobody promised."""

    undocumented = _provider(_completion(_full_stage3()), model_id="groq/compound")

    assert undocumented.output_mode is OutputMode.JSON_OBJECT
    undocumented.analyse(LLMRequest(prompt="p", bundle=None))

    assert undocumented._sent["body"]["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(undocumented._sent["body"])


def test_the_compound_family_is_recorded_as_a_documented_absence():
    from surge.analysis.groq_provider import PUBLISHED_FREE_LIMITS

    compound = PUBLISHED_FREE_LIMITS["groq/compound*"]

    assert compound["output_mode"] is OutputMode.JSON_OBJECT
    assert compound["note"] == JSON_SCHEMA_UNSUPPORTED_BY_CURRENT_DOCS


def test_the_contract_is_checked_here_even_when_the_provider_enforced_it():
    """A schema honoured by the vendor is a convenience. The contract belongs to
    this system, so it is verified on this side in every mode."""

    provider = _provider(_completion({"state": "NOT_A_STATE", "rationale": "x"}))

    with pytest.raises(StructuredOutputError, match="not one of"):
        provider.analyse(LLMRequest(prompt="p", bundle=None))


def test_a_json_object_answer_is_retried_a_bounded_number_of_times():
    attempts = {"n": 0}

    def transport(url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return _Response({"choices": [{"message": {"content": "not json"},
                                           "finish_reason": "stop"}]})
        return _Response(_completion(_full_stage3()))

    provider = GroqHostedProvider(
        model_id="groq/compound",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=MEASURED,
    )

    answer = provider.analyse(LLMRequest(prompt="p", bundle=None))

    assert attempts["n"] == 3
    assert answer.state is Stage3State.REJECT


def test_retry_exhausted_fails_the_analysis_rather_than_proceeding():
    """A malformed answer must not reach a decision. Three tries and it is a
    failed analysis, which is a real outcome with a real record."""

    def transport(url, **kwargs):
        return _Response({"choices": [{"message": {"content": "{\"state\": \"NOPE\"}"},
                                       "finish_reason": "stop"}]})

    provider = GroqHostedProvider(
        model_id="groq/compound",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=MEASURED,
    )

    with pytest.raises(StructuredOutputError, match="after 3 attempt"):
        provider.analyse(LLMRequest(prompt="p", bundle=None))


def test_a_strict_answer_is_not_retried():
    """Retrying a strict-mode failure spends the quota twice for a model that
    was supposed to guarantee the shape."""

    attempts = {"n": 0}

    def transport(url, **kwargs):
        attempts["n"] += 1
        return _Response({"choices": [{"message": {"content": "not json"},
                                       "finish_reason": "stop"}]})

    provider = GroqHostedProvider(
        model_id="openai/gpt-oss-120b",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=MEASURED,
    )

    with pytest.raises(StructuredOutputError):
        provider.analyse(LLMRequest(prompt="p", bundle=None))

    assert attempts["n"] == 1


def test_the_local_schema_check_covers_what_the_contract_uses():
    schema = stage3_schema()

    assert schema_violations(_full_stage3(), schema) == []
    assert any("missing required" in p for p in schema_violations({"state": "REJECT"}, schema))
    assert any(
        "not one of" in p
        for p in schema_violations(_full_stage3(state="WRONG"), schema)
    )
    assert any(
        "not one of" in p
        for p in schema_violations(
            _full_stage3(reachable_zone_basis_kinds=["PRIOR_HIGH"]), schema
        )
    )


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


def test_a_missing_key_names_where_it_actually_comes_from():
    """It used to say ".env.local", and nothing in the worker has ever read that
    file - so following the instruction would have produced the same error."""

    with pytest.raises(CredentialsMissing, match="Set-SurgeSecret"):
        GroqHostedProvider.from_env({ENV_API_KEY: ""})


# ------------------------------------ ZDR is an observation of one account


def test_without_an_observation_zdr_is_unknown_and_production_is_refused():
    """The published terms are the same for every account; whether ZDR is on is
    not. So the code's own answer stays unknown until someone has looked."""

    policy = policy_from_env({})

    assert policy.zero_data_retention_enabled is None
    assert not policy.privacy_gate_passes


def test_an_observation_of_the_console_passes_the_gate_and_says_when():
    policy = policy_from_env({"GROQ_ZDR_CONFIRMED_ON": "2026-09-18"}, today=date(2026, 9, 18))

    assert policy.zero_data_retention_enabled is True
    assert policy.privacy_gate_passes
    assert "2026-09-18" in policy.notes


def test_an_observation_dated_in_the_future_is_refused():
    """Nobody observed a setting tomorrow. Accepting one would make the
    attestation a formality."""

    with pytest.raises(GroqError, match="has not happened yet"):
        policy_from_env({"GROQ_ZDR_CONFIRMED_ON": "2026-09-19"}, today=date(2026, 9, 18))


def test_a_malformed_observation_is_refused_rather_than_ignored():
    with pytest.raises(GroqError, match="ISO date"):
        policy_from_env({"GROQ_ZDR_CONFIRMED_ON": "yes"})


def test_from_env_carries_the_observed_policy_into_the_provider():
    provider = GroqHostedProvider.from_env(
        {ENV_API_KEY: "gsk-x", "GROQ_MODEL": "groq/compound", "GROQ_ZDR_CONFIRMED_ON": "2026-09-18"}
    )

    assert provider.policy.privacy_gate_passes


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
    assert "STRICT_JSON_SCHEMA" in row["notes"]


# ------------------------------- the built-in tools that are on by default


def test_a_compound_request_asks_for_the_tools_to_be_off():
    """Groq's compound systems have web search, visiting a website, running code
    and Wolfram Alpha enabled by default. During an analysis that is not a
    feature: it would read information from after the decision cutoff, from
    sources in no bundle and no hash."""

    provider = _provider(_completion(_full_stage3()), model_id="groq/compound")
    provider.analyse(LLMRequest(prompt="p", bundle=None))
    body = provider._sent["body"]

    assert body["tool_choice"] == "none"
    assert body["compound_custom"] == {"tools": {"enabled_tools": []}}


def test_a_gpt_oss_request_does_not_carry_tool_settings_it_does_not_need():
    provider = _provider(_completion(_full_stage3()))
    provider.analyse(LLMRequest(prompt="p", bundle=None))

    assert "tool_choice" not in provider._sent["body"]
    assert "compound_custom" not in provider._sent["body"]


def test_a_response_reporting_an_executed_tool_fails_the_analysis():
    """The request asks for tools to be off and Groq documents no way to switch
    them off for compound, so the request is best effort. This check is what
    actually enforces the rule."""

    payload = _completion(_full_stage3())
    payload["choices"][0]["message"]["executed_tools"] = [
        {"type": "web_search", "arguments": "{}"}
    ]
    provider = _provider(payload, model_id="groq/compound")

    with pytest.raises(ExternalToolUsed, match=ANALYSIS_EXTERNAL_TOOL_USED):
        provider.analyse(LLMRequest(prompt="p", bundle=None))


def test_an_executed_tool_at_the_top_level_is_caught_too():
    """Where the provider puts the field is not something to be confident about,
    and missing it means an answer built on unrecorded sources is treated as if
    it came from the bundle."""

    payload = _completion(_full_stage3())
    payload["executed_tools"] = [{"type": "visit_website"}]
    provider = _provider(payload, model_id="groq/compound")

    with pytest.raises(ExternalToolUsed, match="visit_website"):
        provider.analyse(LLMRequest(prompt="p", bundle=None))


def test_the_tool_check_applies_to_every_model_not_just_compound():
    """A gpt-oss deployment that grew tools later would otherwise slip through."""

    payload = _completion(_full_stage3())
    payload["choices"][0]["message"]["executed_tools"] = [{"type": "code_interpreter"}]
    provider = _provider(payload)

    with pytest.raises(ExternalToolUsed):
        provider.analyse(LLMRequest(prompt="p", bundle=None))


def test_an_empty_executed_tools_list_is_not_a_violation():
    payload = _completion(_full_stage3())
    payload["choices"][0]["message"]["executed_tools"] = []
    provider = _provider(payload, model_id="groq/compound")

    assert provider.analyse(LLMRequest(prompt="p", bundle=None)).state is Stage3State.REJECT


def test_which_models_carry_built_in_tools():
    assert uses_built_in_tools("groq/compound")
    assert uses_built_in_tools("groq/compound-mini")
    assert not uses_built_in_tools("openai/gpt-oss-120b")


def test_the_probe_is_also_refused_an_answer_that_used_tools():
    """The probe reads headers, and an answer produced with tools still means
    the account is configured in a way this analysis cannot use."""

    def transport(url, **kwargs):
        payload = _completion({"ok": True})
        payload["choices"][0]["message"]["executed_tools"] = [{"type": "web_search"}]
        return _Response(payload, headers={"x-ratelimit-limit-tokens": "8000"})

    provider = GroqHostedProvider(
        model_id="groq/compound",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=Quota(),
    )

    # The probe only reads headers, so it succeeds - and the quota it learns is
    # real. The tool check bites when an actual analysis is run.
    measured = provider.quota_probe()
    assert measured.max_tokens_per_minute == 8_000


# ------------------------------- the production privacy gate (Audit 5 G)


def test_the_real_account_cannot_receive_a_production_canonical_request():
    """The gate is not decorative. Groq's published terms say inputs are not
    trained on, which is what makes an experiment acceptable; routine production
    traffic carries Canonical v5.1 in full, several times a day, for as long as
    the system runs, and for that ZDR has to be confirmed *on this account*.

    Nobody has looked at the account, so this refuses - and that is the correct
    state today, not a bug to be worked around."""

    provider = _provider(_completion(_full_stage3()), policy=GROQ_POLICY)

    with pytest.raises(InputPolicyViolation, match="ANALYSIS_PRIVACY_GATE_BLOCKED"):
        provider.analyse(LLMRequest(prompt="the canonical method", bundle=None))


def test_unknown_zdr_is_refused_as_firmly_as_zdr_switched_off():
    """Silence is not consent. "Nobody has checked" and "it is off" are
    different facts about the world and the same fact about what may be sent."""

    off = replace(GROQ_POLICY, zero_data_retention_enabled=False)
    unknown = replace(GROQ_POLICY, zero_data_retention_enabled=None)

    for policy in (off, unknown):
        with pytest.raises(InputPolicyViolation, match="ANALYSIS_PRIVACY_GATE_BLOCKED"):
            policy.assert_production_privacy_gate_passes()

    replace(GROQ_POLICY, zero_data_retention_enabled=True).assert_production_privacy_gate_passes()


def test_a_research_smoke_passes_the_weaker_gate_and_is_a_different_mode():
    """A smoke sends a fixed harmless prompt and never the method, so the gate
    it needs is the one about training rather than the one about retention. It
    is a mode rather than a flag, because a relaxation nobody names is one
    nobody notices."""

    provider = _provider(
        _completion(_full_stage3()), policy=GROQ_POLICY, mode=RequestMode.RESEARCH_SMOKE
    )

    provider.analyse(LLMRequest(prompt="a harmless probe", bundle=None))

    assert provider._sent["body"]["model"]


def test_production_is_the_default_mode():
    """A smoke that forgot to say so would otherwise send the canonical method
    under the weaker gate."""

    assert GroqHostedProvider(model_id="openai/gpt-oss-120b", api_key="x").mode is (
        RequestMode.PRODUCTION
    )


# --------------------------- the tool ban, on the probe too (Audit 5 F)


def test_the_quota_probe_also_refuses_tools():
    """Its answer is discarded, but a tool call would cost money, leave a trace
    at a third party, and make the measured token count describe a request
    nothing else will ever send."""

    sent = {}

    def transport(url, **kwargs):
        import json as _json

        sent["body"] = _json.loads(kwargs["data"])
        return _Response(_completion({"ok": True}), headers={"x-ratelimit-limit-tokens": "8000"})

    provider = GroqHostedProvider(
        model_id="groq/compound",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=transport,
        quota=Quota(),
    )

    provider.quota_probe()

    body = sent["body"]
    assert body["tool_choice"] == "none"
    assert body["compound_custom"] == {"tools": {"enabled_tools": []}}


# ------------------------- json_object requests have to say "json" (live 400)


def _groq_rule(body: dict) -> None:
    """What Groq does, found by the first live request: a json_object response
    format is refused unless some message contains the word json. Written down
    here because the fake transport used to accept anything, which is how a path
    that could never have worked passed every test."""

    if (body.get("response_format") or {}).get("type") == "json_object":
        joined = " ".join(m.get("content") or "" for m in body.get("messages") or [])
        if "json" not in joined.lower():
            raise AssertionError(
                "'messages' must contain the word 'json' in some form, to use "
                "'response_format' of type 'json_object'"
            )


def test_a_json_object_request_satisfies_groqs_json_rule():
    provider = _provider(_completion(_full_entry()), model_id="groq/compound")

    provider.analyse_entry(LLMRequest(prompt="the rendered entry prompt", bundle=None))

    body = provider._sent["body"]
    assert body["response_format"] == {"type": "json_object"}
    _groq_rule(body)


def test_the_schema_travels_in_its_own_message_and_the_prompt_is_untouched():
    """The prompt is what prompt_sha256 is taken over at TX1, so it has to be
    the same whichever output mode carries it. The schema goes beside it."""

    provider = _provider(_completion(_full_entry()), model_id="groq/compound")

    provider.analyse_entry(LLMRequest(prompt="the rendered entry prompt", bundle=None))

    messages = provider._sent["body"]["messages"]
    assert messages[0]["role"] == "system"
    assert '"state"' in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "the rendered entry prompt"}


def test_a_strict_request_sends_the_prompt_alone():
    """Structured Outputs enforces the schema itself; nothing is added."""

    provider = _provider(_completion(_full_entry()), model_id="openai/gpt-oss-120b")

    provider.analyse_entry(LLMRequest(prompt="the rendered entry prompt", bundle=None))

    assert provider._sent["body"]["messages"] == [
        {"role": "user", "content": "the rendered entry prompt"}
    ]


def test_the_preflight_counts_the_framing_as_well_as_the_prompt():
    """A preflight that left the schema out would pass a request the account
    refuses."""

    provider = _provider(_completion(_full_entry()), model_id="groq/compound")

    provider.analyse_entry(LLMRequest(prompt="x", bundle=None))

    framing = GroqHostedProvider.json_object_framing(entry_analysis_schema())
    assert provider.last_preflight.prompt_tokens_estimated > len(framing) // 8


# ---------------------------- every HTTP status says whether it can recover


class _HttpErrorTransport:
    """Raises the way the real transport does for a 4xx: urllib's HTTPError."""

    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self.body = body or {}

    def __call__(self, url, **kwargs):
        import io
        import urllib.error

        raise urllib.error.HTTPError(
            url, self.status, "error", {}, io.BytesIO(json.dumps(self.body).encode())
        )


def _provider_raising(status: int, body: dict | None = None) -> GroqHostedProvider:
    provider = GroqHostedProvider(
        model_id="groq/compound",
        api_key="gsk-not-a-real-key",
        policy=ZDR_CONFIRMED,
        transport=_HttpErrorTransport(status, body),
        quota=Quota(max_tokens_per_minute=70_000),
    )
    return provider


@pytest.mark.parametrize(
    ("status", "failure_class", "transient"),
    [
        (413, FailureClass.QUOTA_BLOCKED, False),
        (400, FailureClass.CONTRACT_VIOLATION, False),
        (422, FailureClass.CONTRACT_VIOLATION, False),
        (401, FailureClass.PROVIDER_NOT_CONFIGURED, False),
        (403, FailureClass.PROVIDER_NOT_CONFIGURED, False),
        (404, FailureClass.PROVIDER_NOT_CONFIGURED, False),
    ],
)
def test_a_client_error_is_terminal_and_says_which_kind(status, failure_class, transient):
    """urllib's HTTPError is an OSError, and an OSError reads as the network -
    so without this a 413 or a 400 would be retried as a dropped connection,
    five times, sending the same refused request each time."""

    provider = _provider_raising(status)

    with pytest.raises(GroqError) as caught:
        provider.analyse_entry(LLMRequest(prompt="p", bundle=None))

    assert classify_provider_failure(caught.value) is failure_class
    assert failure_class.is_transient is transient


def test_a_413_is_the_one_found_live_and_it_carries_groqs_explanation():
    provider = _provider_raising(
        413,
        {"error": {"type": "invalid_request_error", "code": "request_too_large",
                   "message": "Request Entity Too Large"}},
    )

    with pytest.raises(RequestTooLarge, match="request_too_large"):
        provider.analyse_entry(LLMRequest(prompt="p", bundle=None))


def test_a_rate_limit_the_transport_gave_up_on_is_still_a_rate_limit():
    """The transport retries a 429 itself and then raises a RuntimeError. The
    status survives as the chained cause, so this is retried later rather than
    failed now."""

    import io
    import urllib.error

    def exhausted(url, **kwargs):
        last = urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
        raise RuntimeError(f"fetch failed after 3 attempts: {url}: {last}") from last

    provider = GroqHostedProvider(
        model_id="groq/compound", api_key="gsk-not-a-real-key", policy=ZDR_CONFIRMED,
        transport=exhausted, quota=Quota(max_tokens_per_minute=70_000),
    )

    with pytest.raises(RateLimited) as caught:
        provider.analyse_entry(LLMRequest(prompt="p", bundle=None))

    assert classify_provider_failure(caught.value) is FailureClass.PROVIDER_RATE_LIMITED
    assert FailureClass.PROVIDER_RATE_LIMITED.is_transient


def test_a_server_error_is_transient():
    assert classify_provider_failure(failure_for_status(503, "")) is FailureClass.PROVIDER_ERROR
    assert FailureClass.PROVIDER_ERROR.is_transient
