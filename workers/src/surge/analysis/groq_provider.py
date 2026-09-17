"""Groq: a hosted model that is contractually not allowed to learn from us.

The choice of provider here was not made on quality. The bundle sent to Stage 3
and to the intraday entry analysis contains Canonical v5.1 - the user's own
method, in full, on every request. What a provider is permitted to do with that
text matters more than how well it reasons about it, and the two candidates part
company on exactly that point:

*Google Gemini, free tier*
    "Google uses the content you submit to the Services and any generated
    responses to provide, improve, and develop Google products and services" and
    "human reviewers may read, annotate, and process your API input and output"
    (https://ai.google.dev/gemini-api/terms). The paid tier differs; paid is out
    of scope under Zero-Cost Core.

*Groq*
    "Groq is not permitted to use Inputs or Outputs for training or fine-tuning
    any AI Model Services or other models, unless explicitly granted permission
    or instructed by Customer" (Services Agreement), and "By default, Groq does
    not retain customer data for inference requests", with Zero Data Retention
    available in Data Controls (https://console.groq.com/docs/your-data). The
    retention page draws no line between the free and paid tiers.

So the module encodes the policy as data and refuses to send the canonical
prompt to a provider whose terms do not say, in so many words, that inputs are
not trained on. Silence is not consent here either.

Two other things are deliberate:

**The model is configuration, not code.** Groq's catalogue turns over faster than
this repository does. A model id belongs in ``GROQ_MODEL``; what belongs here is
the contract it has to satisfy.

**There is no truncation.** When a bundle does not fit the free tier, the answer
is ``D-32_BLOCKED_FREE_QUOTA``, not a shorter canonical prompt. Trimming v5.1 to
fit a quota would make every answer an answer to a different method, and the
difference would not be visible in the output.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from surge.analysis.bundle import canonical_json
from surge.analysis.entry_analysis import EntryAnalysisResponse, EntryAnalysisState
from surge.analysis.llm import LLMRequest, LLMResponse, ProviderKind, Stage3State, ZoneBasisKind
from surge.http_fetch import fetch

GROQ_BASE = "https://api.groq.com/openai/v1"
PROVIDER_ID = "groq_hosted"
PROVIDER_VERSION = "groq-hosted-1.0.0"

ENV_API_KEY = "GROQ_API_KEY"
ENV_MODEL = "GROQ_MODEL"
ENV_STRICT_MODELS = "GROQ_STRICT_MODELS"

#: Models Groq documents as supporting ``strict``. Overridable through
#: ``GROQ_STRICT_MODELS`` because this list changes without warning; the code
#: never requires a particular model, only a particular contract.
DOCUMENTED_STRICT_MODELS = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})

#: The decision id to raise when the canonical prompt plus the bundle will not
#: fit whatever the account is actually allowed.
BLOCKED_FREE_QUOTA = "D-32_BLOCKED_FREE_QUOTA"


class GroqError(RuntimeError):
    pass


class CredentialsMissing(GroqError):
    """No API key. The adapter exists and has never spoken to Groq."""


class FreeQuotaExceeded(GroqError):
    """The request does not fit. The prompt is not the thing that gives way."""


class InputPolicyViolation(GroqError):
    """Refusing to send the canonical method to a provider that may train on it."""


class StructuredOutputError(GroqError):
    """The model returned something the contract cannot read."""


# ------------------------------------------------------------- data policy


class InputUse(StrEnum):
    """What a provider's terms say it may do with what we send.

    Four states for the same reason the licence vocabulary has four: a provider
    that has not addressed the question has not agreed to anything, and
    NOT_SPECIFIED must not collapse into permission in either direction.
    """

    NOT_USED_FOR_TRAINING = "NOT_USED_FOR_TRAINING"
    USED_FOR_TRAINING = "USED_FOR_TRAINING"
    NOT_SPECIFIED = "NOT_SPECIFIED"
    UNKNOWN = "UNKNOWN"


class Retention(StrEnum):
    NONE_BY_DEFAULT = "NONE_BY_DEFAULT"
    TRANSIENT_FOR_ABUSE_AND_RELIABILITY = "TRANSIENT_FOR_ABUSE_AND_RELIABILITY"
    RETAINED = "RETAINED"
    NOT_SPECIFIED = "NOT_SPECIFIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DataPolicy:
    """One reading of one model provider's terms, with the source.

    Every field is quotable from a published page. A policy assembled from
    recollection or from a summary is how a provider's terms get remembered more
    favourably than they were written.
    """

    provider_id: str
    input_use: InputUse
    retention: Retention
    zero_data_retention_available: bool
    zero_data_retention_enabled: bool | None
    terms_url: str
    retention_url: str | None = None
    tier_distinction: str | None = None
    notes: str | None = None

    @property
    def may_receive_the_canonical_method(self) -> bool:
        return self.input_use is InputUse.NOT_USED_FOR_TRAINING

    def assert_may_receive_the_canonical_method(self) -> None:
        if self.may_receive_the_canonical_method:
            return
        raise InputPolicyViolation(
            f"{self.provider_id} is recorded as {self.input_use.value} for submitted inputs. Every "
            "request carries Canonical v5.1 in full, so sending it would hand over the method "
            f"itself. See {self.terms_url}"
        )


#: Groq, from the Services Agreement and the data page. ZDR is recorded as
#: "available, not known to be enabled": whether it is switched on is a fact
#: about the account, and this code has never seen the account.
GROQ_POLICY = DataPolicy(
    provider_id=PROVIDER_ID,
    input_use=InputUse.NOT_USED_FOR_TRAINING,
    retention=Retention.NONE_BY_DEFAULT,
    zero_data_retention_available=True,
    zero_data_retention_enabled=None,
    terms_url="https://groq.com/terms-of-sale/",
    retention_url="https://console.groq.com/docs/your-data",
    tier_distinction="the retention page draws no distinction between the free and paid tiers",
    notes=(
        "Services Agreement: 'Groq is not permitted to use Inputs or Outputs for training or "
        "fine-tuning any AI Model Services or other models, unless explicitly granted permission or "
        "instructed by Customer.' Data page: 'By default, Groq does not retain customer data for "
        "inference requests', with transient retention possible for reliability and abuse handling, "
        "and Zero Data Retention selectable in Data Controls."
    ),
)

#: Kept beside it as the comparison that decided the question, and as a live
#: guard: a future adapter that tried to use the free Gemini tier for Stage 3
#: would be refused rather than reviewed.
GEMINI_FREE_TIER_POLICY = DataPolicy(
    provider_id="google_gemini_free",
    input_use=InputUse.USED_FOR_TRAINING,
    retention=Retention.RETAINED,
    zero_data_retention_available=False,
    zero_data_retention_enabled=False,
    terms_url="https://ai.google.dev/gemini-api/terms",
    tier_distinction="the unpaid tier only; the paid tier is governed differently and is out of "
    "scope under Zero-Cost Core",
    notes=(
        "'Google uses the content you submit to the Services and any generated responses to "
        "provide, improve, and develop Google products and services' and 'human reviewers may read, "
        "annotate, and process your API input and output'."
    ),
)

PRODUCTION_RECOMMENDED_SETTING = (
    "enable Zero Data Retention in Groq's Data Controls before the first production request"
)


# ---------------------------------------------------------- token preflight

#: Rough, and rough in the safe direction. A CJK character is usually close to
#: one token and Latin text closer to four characters per token, so counting
#: them separately keeps a Japanese canonical prompt from being estimated at a
#: quarter of its real size. The estimate is only ever used to refuse a request
#: before sending it; the authoritative number is the ``usage`` block that comes
#: back, which is recorded on the response.
CHARS_PER_TOKEN_LATIN = 3.5


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # kana
        or 0x3400 <= code <= 0x4DBF  # CJK ext A
        or 0x4E00 <= code <= 0x9FFF  # CJK unified
        or 0xF900 <= code <= 0xFAFF  # compatibility ideographs
        or 0xFF66 <= code <= 0xFF9F  # halfwidth kana
    )


def estimate_tokens(text: str) -> int:
    """An upper-leaning estimate, for deciding whether to send at all."""

    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return int(cjk + other / CHARS_PER_TOKEN_LATIN) + 1


@dataclass(frozen=True)
class Quota:
    """What the account is actually allowed.

    Not defaulted to a published figure. Groq's free tier limits differ per model
    and per account, and a preflight run against a number from a documentation
    page would pass right up until the moment it did not.
    """

    max_input_tokens_per_request: int | None = None
    max_tokens_per_minute: int | None = None
    max_input_tokens_per_minute: int | None = None
    max_requests_per_minute: int | None = None
    source: str = "not measured; read the account's rate-limit headers"

    @property
    def is_known(self) -> bool:
        return any(
            value is not None
            for value in (
                self.max_input_tokens_per_request,
                self.max_tokens_per_minute,
                self.max_input_tokens_per_minute,
            )
        )


@dataclass(frozen=True)
class Preflight:
    prompt_tokens_estimated: int
    quota: Quota
    fits: bool
    reason: str
    decision_id: str | None = None

    @property
    def summary(self) -> dict:
        return {
            "prompt_tokens_estimated": self.prompt_tokens_estimated,
            "fits": self.fits,
            "reason": self.reason,
            "decision": self.decision_id,
            "quota_source": self.quota.source,
        }


def preflight(prompt: str, *, quota: Quota, requests_per_day: int = 1) -> Preflight:
    """Decide whether this prompt may be sent, without changing the prompt.

    There is no ``max_chars`` and no truncation argument, by design. If the
    bundle does not fit, the finding is that the free tier cannot run this
    analysis - which is a fact worth having - and not that v5.1 needs shortening.
    """

    estimated = estimate_tokens(prompt)

    if not quota.is_known:
        return Preflight(
            prompt_tokens_estimated=estimated,
            quota=quota,
            fits=False,
            reason=(
                f"the account's limits have not been measured, so whether ~{estimated} input tokens "
                "fit is unknown. Read them from the rate-limit headers of one real request before "
                "running a batch"
            ),
            decision_id=None,
        )

    per_request = quota.max_input_tokens_per_request
    if per_request is not None and estimated > per_request:
        return Preflight(
            prompt_tokens_estimated=estimated,
            quota=quota,
            fits=False,
            reason=(
                f"~{estimated} input tokens against a per-request limit of {per_request}. The "
                "canonical prompt is not shortened to fit a quota: every answer would then be an "
                "answer to a different method, and nothing in the output would show it"
            ),
            decision_id=BLOCKED_FREE_QUOTA,
        )

    per_minute = quota.max_input_tokens_per_minute or quota.max_tokens_per_minute
    if per_minute is not None and estimated > per_minute:
        return Preflight(
            prompt_tokens_estimated=estimated,
            quota=quota,
            fits=False,
            reason=(
                f"~{estimated} input tokens exceeds the whole per-minute allowance ({per_minute}); "
                "a single request cannot be sent at all"
            ),
            decision_id=BLOCKED_FREE_QUOTA,
        )

    if per_minute is not None and requests_per_day > 1:
        minutes = (estimated * requests_per_day) / per_minute
        return Preflight(
            prompt_tokens_estimated=estimated,
            quota=quota,
            fits=True,
            reason=(
                f"~{estimated} input tokens each; {requests_per_day} of them need about "
                f"{minutes:.1f} minute(s) of the per-minute allowance, so the batch has to be paced"
            ),
        )

    return Preflight(
        prompt_tokens_estimated=estimated,
        quota=quota,
        fits=True,
        reason=f"~{estimated} input tokens is inside the measured limits",
    )


# ------------------------------------------------------- structured outputs


def _enum_list(values) -> list[str]:
    return [value.value for value in values]


def _nullable(kind: str) -> dict:
    """Strict mode has no optional properties; an absent value is an explicit null."""

    return {"type": [kind, "null"]}


def stage3_schema() -> dict:
    """The Stage 3 contract, as JSON Schema.

    Matches ``LLMResponse`` field for field. A schema that drifted from the
    dataclass would produce answers that validate and then cannot be stored.
    """

    properties = {
        "state": {"type": "string", "enum": _enum_list(Stage3State)},
        "rationale": {"type": "string"},
        "confidence_note": _nullable("string"),
        "reachable_zone_low": _nullable("number"),
        "reachable_zone_high": _nullable("number"),
        "reachable_zone_basis_kinds": {
            "type": "array",
            "items": {"type": "string", "enum": _enum_list(ZoneBasisKind)},
        },
        "reachable_zone_basis": _nullable("string"),
        "concepts_considered": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


def entry_analysis_schema() -> dict:
    """The intraday contract, as JSON Schema."""

    properties = {
        "state": {"type": "string", "enum": _enum_list(EntryAnalysisState)},
        "rationale": {"type": "string"},
        "decision_price_used": _nullable("number"),
        "proposed_initial_failure_line": _nullable("number"),
        "reachable_zone_low": _nullable("number"),
        "reachable_zone_high": _nullable("number"),
        "reachable_zone_basis_kinds": {
            "type": "array",
            "items": {"type": "string", "enum": _enum_list(ZoneBasisKind)},
        },
        "reachable_zone_basis": _nullable("string"),
        "concepts_considered": {"type": "array", "items": {"type": "string"}},
        "watch_trigger_description": _nullable("string"),
        "reject_reason": _nullable("string"),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


def response_format(name: str, schema: dict, *, strict: bool) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": strict, "schema": schema},
    }


def strict_models(env: dict[str, str] | None = None) -> frozenset[str]:
    source = env if env is not None else os.environ
    configured = (source.get(ENV_STRICT_MODELS) or "").strip()
    if configured:
        return frozenset(part.strip() for part in configured.split(",") if part.strip())
    return DOCUMENTED_STRICT_MODELS


# -------------------------------------------------------------- the provider


@dataclass(frozen=True)
class GroqUsage:
    """What the request actually cost, as the API reported it."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> GroqUsage:
        usage = payload.get("usage") or {}
        return cls(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )


@dataclass
class GroqHostedProvider:
    """A real analysis provider for both contracts.

    Speaks Stage 3 through :meth:`analyse` and the intraday entry contract
    through :meth:`analyse_entry`, so one credential covers both. The provider
    kind is ``HOSTED_LLM``, which is what lets a prediction come from it at all -
    the database refuses one whose provider kind is ``DETERMINISTIC_MOCK``.
    """

    model_id: str
    api_key: str = field(repr=False, default="")
    provider_id: str = PROVIDER_ID
    provider_kind: ProviderKind = ProviderKind.HOSTED_LLM
    policy: DataPolicy = GROQ_POLICY
    quota: Quota = field(default_factory=Quota)
    temperature: float = 0.0
    max_completion_tokens: int = 2048
    base_url: str = GROQ_BASE
    version: str = PROVIDER_VERSION
    transport: Any = fetch
    strict_model_ids: frozenset[str] = field(default_factory=DOCUMENTED_STRICT_MODELS.copy)
    last_usage: GroqUsage | None = field(default=None, repr=False)
    last_preflight: Preflight | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **overrides) -> GroqHostedProvider:
        source = env if env is not None else os.environ
        key = (source.get(ENV_API_KEY) or "").strip()
        model = (source.get(ENV_MODEL) or "").strip()
        if not key:
            raise CredentialsMissing(
                f"{ENV_API_KEY} is not set. Put it in .env.local - never in the repository, never "
                "in a chat message - and re-run. Until then the analysis provider is the "
                "deterministic stand-in, which cannot produce a formal prediction"
            )
        if not model:
            raise GroqError(
                f"{ENV_MODEL} is not set. The model is configuration, not code: name the one this "
                "run should use so the output row records which model answered"
            )
        return cls(
            model_id=model,
            api_key=key,
            strict_model_ids=strict_models(source),
            **overrides,
        )

    @property
    def supports_strict(self) -> bool:
        return self.model_id in self.strict_model_ids

    # ------------------------------------------------------------- requests

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _call(self, prompt: str, *, schema_name: str, schema: dict) -> dict:
        self.policy.assert_may_receive_the_canonical_method()

        check = preflight(prompt, quota=self.quota)
        self.last_preflight = check
        if not check.fits and check.decision_id == BLOCKED_FREE_QUOTA:
            raise FreeQuotaExceeded(f"{check.decision_id}: {check.reason}")

        body = json.dumps(
            {
                "model": self.model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_completion_tokens": self.max_completion_tokens,
                "response_format": response_format(
                    schema_name, schema, strict=self.supports_strict
                ),
            }
        ).encode("utf-8")

        response = self.transport(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            data=body,
            method="POST",
        )
        if response.status != 200:
            raise GroqError(f"groq returned {response.status}")
        payload = json.loads(response.body.decode("utf-8"))
        self.last_usage = GroqUsage.from_payload(payload)

        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as exc:
            raise StructuredOutputError(f"no choice in the groq response: {exc}") from exc
        if message.get("refusal"):
            raise StructuredOutputError(f"the model refused: {message['refusal']}")
        content = message.get("content")
        if not content:
            raise StructuredOutputError(
                f"empty content (finish_reason={choice.get('finish_reason')!r}). A truncated "
                "structured output is not a partial answer, it is not an answer"
            )
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            # Structured outputs make this unlikely and not impossible, and a
            # half-parsed answer must never become a prediction.
            raise StructuredOutputError(f"the content was not JSON: {exc}") from exc
        parsed["_raw"] = content
        return parsed

    # -------------------------------------------------------------- stage 3

    def analyse(self, request: LLMRequest) -> LLMResponse:
        parsed = self._call(
            request.prompt, schema_name="stage3_analysis", schema=stage3_schema()
        )
        kinds = tuple(
            ZoneBasisKind(value) for value in parsed.get("reachable_zone_basis_kinds") or ()
        )
        return LLMResponse(
            state=Stage3State(parsed["state"]),
            rationale=parsed.get("rationale") or "",
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            model_id=self.model_id,
            confidence_note=parsed.get("confidence_note"),
            reachable_zone_low=parsed.get("reachable_zone_low"),
            reachable_zone_high=parsed.get("reachable_zone_high"),
            reachable_zone_basis_kinds=kinds,
            reachable_zone_basis=parsed.get("reachable_zone_basis"),
            concepts_considered=tuple(parsed.get("concepts_considered") or ()),
            raw_text=parsed["_raw"],
        )

    # ------------------------------------------------------------- intraday

    def analyse_entry(self, request: LLMRequest) -> EntryAnalysisResponse:
        parsed = self._call(
            request.prompt, schema_name="entry_analysis", schema=entry_analysis_schema()
        )
        kinds = tuple(
            ZoneBasisKind(value) for value in parsed.get("reachable_zone_basis_kinds") or ()
        )
        return EntryAnalysisResponse(
            state=EntryAnalysisState(parsed["state"]),
            rationale=parsed.get("rationale") or "",
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            model_id=self.model_id,
            decision_price_used=_decimal(parsed.get("decision_price_used")),
            proposed_initial_failure_line=_decimal(parsed.get("proposed_initial_failure_line")),
            reachable_zone_low=_decimal(parsed.get("reachable_zone_low")),
            reachable_zone_high=_decimal(parsed.get("reachable_zone_high")),
            reachable_zone_basis_kinds=kinds,
            reachable_zone_basis=parsed.get("reachable_zone_basis"),
            concepts_considered=tuple(parsed.get("concepts_considered") or ()),
            watch_trigger_description=parsed.get("watch_trigger_description"),
            reject_reason=parsed.get("reject_reason"),
            raw_text=parsed["_raw"],
        )

    # ---------------------------------------------------------------- record

    def registry_row(self, *, live_verified_at: datetime | None = None) -> dict:
        """What ``analysis.llm_providers`` should hold for this provider."""

        return {
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind.value,
            "name": "Groq (hosted, developer tier)",
            "model_id": self.model_id,
            "required_env": [ENV_API_KEY, ENV_MODEL],
            "monthly_cost_jpy": 0,
            "enabled": False,
            "live_verified_at": live_verified_at,
            "notes": canonical_json(
                {
                    "input_use": self.policy.input_use.value,
                    "retention": self.policy.retention.value,
                    "zero_data_retention_available": self.policy.zero_data_retention_available,
                    "zero_data_retention_enabled": self.policy.zero_data_retention_enabled,
                    "terms_url": self.policy.terms_url,
                    "retention_url": self.policy.retention_url,
                    "production_recommended": PRODUCTION_RECOMMENDED_SETTING,
                    "strict_structured_outputs": self.supports_strict,
                    "recorded_at": datetime.now(UTC).date().isoformat(),
                }
            ),
        }


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


__all__ = [
    "BLOCKED_FREE_QUOTA",
    "DOCUMENTED_STRICT_MODELS",
    "ENV_API_KEY",
    "ENV_MODEL",
    "GEMINI_FREE_TIER_POLICY",
    "GROQ_POLICY",
    "PRODUCTION_RECOMMENDED_SETTING",
    "PROVIDER_ID",
    "CredentialsMissing",
    "DataPolicy",
    "FreeQuotaExceeded",
    "GroqError",
    "GroqHostedProvider",
    "GroqUsage",
    "InputPolicyViolation",
    "InputUse",
    "Preflight",
    "Quota",
    "Retention",
    "StructuredOutputError",
    "entry_analysis_schema",
    "estimate_tokens",
    "preflight",
    "response_format",
    "stage3_schema",
    "strict_models",
]
