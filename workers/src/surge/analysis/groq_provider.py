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
is ``ANALYSIS_FREE_QUOTA_BLOCKED``, not a shorter canonical prompt. Trimming v5.1 to
fit a quota would make every answer an answer to a different method, and the
difference would not be visible in the output.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from surge.analysis.bundle import canonical_json
from surge.analysis.entry_analysis import EntryAnalysisResponse, EntryAnalysisState
from surge.analysis.execution import FailureClass, ProviderFailure
from surge.analysis.llm import LLMRequest, LLMResponse, ProviderKind, Stage3State, ZoneBasisKind
from surge.http_fetch import fetch

GROQ_BASE = "https://api.groq.com/openai/v1"
PROVIDER_ID = "groq_hosted"
PROVIDER_VERSION = "groq-hosted-1.1.0"

ENV_API_KEY = "GROQ_API_KEY"
ENV_MODEL = "GROQ_MODEL"
ENV_STRICT_MODELS = "GROQ_STRICT_MODELS"
#: The date someone looked at this account's Data Controls page and saw Zero
#: Data Retention switched on. Groq publishes no API for reading that setting,
#: so the only evidence there can be is an observation, and it is recorded as
#: one - dated, and carried into the policy's notes - rather than as a boolean
#: that would read the same whether it was checked yesterday or never.
ENV_ZDR_CONFIRMED_ON = "GROQ_ZDR_CONFIRMED_ON"

#: Models Groq documents as supporting ``strict``. Overridable through
#: ``GROQ_STRICT_MODELS`` because this list changes without warning; the code
#: never requires a particular model, only a particular contract.
DOCUMENTED_STRICT_MODELS = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})

#: A runtime reason code, not a decision id. The decisions are D-189 (real EOD /
#: Stage 3 provider) and D-190 (real intraday entry provider); this is what the
#: preflight reports when the canonical prompt plus the bundle will not fit
#: whatever the account is actually allowed.
ANALYSIS_FREE_QUOTA_BLOCKED = "ANALYSIS_FREE_QUOTA_BLOCKED"

class RequestMode(StrEnum):
    """What a request is for, which decides which privacy gate applies.

    Kept explicit rather than inferred from a flag, because the two differ in
    what they are allowed to send and an inferred mode is one nobody reads.
    """

    #: Carries Canonical v5.1, the addenda and a real bundle. Needs the
    #: production gate: not trained on *and* ZDR confirmed on for the account.
    PRODUCTION = "PRODUCTION"
    #: Carries a fixed harmless prompt and no method, no security, no price.
    #: Used to find out whether the adapter can talk to the provider at all.
    RESEARCH_SMOKE = "RESEARCH_SMOKE"


class OutputMode(StrEnum):
    """What a model can be asked to return, as its vendor documents it today.

    Four states rather than a boolean, because "not strict" covers two very
    different situations: a model that will honour a schema on a best-effort
    basis, and one that will only promise valid JSON with no schema at all. The
    second still works here - the schema is re-checked on this side - but it
    needs a retry loop, and knowing which one is in use is what decides whether
    that loop exists.
    """

    STRICT_JSON_SCHEMA = "STRICT_JSON_SCHEMA"
    BEST_EFFORT_JSON_SCHEMA = "BEST_EFFORT_JSON_SCHEMA"
    JSON_OBJECT = "JSON_OBJECT"
    PLAIN_TEXT = "PLAIN_TEXT"

    @property
    def sends_a_schema(self) -> bool:
        return self in (OutputMode.STRICT_JSON_SCHEMA, OutputMode.BEST_EFFORT_JSON_SCHEMA)

    @property
    def needs_client_side_retry(self) -> bool:
        """Whether a malformed answer is expected often enough to plan for."""

        return self is OutputMode.JSON_OBJECT


#: Recorded when a family is absent from the vendor's structured-outputs list.
#: Deliberately not "unverified": the documentation is a positive list, and a
#: family missing from it is a documented absence rather than an open question.
#: What remains open is whether json_object plus a client-side schema check is
#: good enough, and that is a different thing to find out.
JSON_SCHEMA_UNSUPPORTED_BY_CURRENT_DOCS = "JSON_SCHEMA_UNSUPPORTED_BY_CURRENT_DOCS"

#: How many completion tokens a request reserves. Groq's free tier meters one
#: combined tokens-per-minute figure, so the reserve counts against the same
#: allowance as the prompt and a preflight that ignored it would approve
#: requests the API then refuses.
DEFAULT_COMPLETION_RESERVE = 2048

#: How many times a json_object answer may be re-requested before the analysis
#: is failed. Bounded, and small: a model that cannot produce the shape twice is
#: not going to produce it on the ninth attempt, and every attempt spends the
#: same quota the prompt does.
MAX_JSON_OBJECT_ATTEMPTS = 3


#: Groq's published free-plan limits, read from
#: https://console.groq.com/docs/rate-limits on 2026-09-17. Recorded per model
#: family rather than as one number for the provider, because they differ by an
#: order of magnitude and collapsing them is exactly the mistake that made
#: D-103 wrong: one endpoint group's constraint generalised to a whole vendor.
#:
#: Published limits are a starting point and not the account's. They decide
#: whether an account is worth opening; the account's own headers decide whether
#: anything is sent.
PUBLISHED_FREE_LIMITS = {
    "openai/gpt-oss-*": {
        "requests_per_minute": 30,
        "requests_per_day": 1_000,
        "tokens_per_minute": 8_000,
        "tokens_per_day": 200_000,
        "output_mode": OutputMode.STRICT_JSON_SCHEMA,
        "tokenizer": "o200k_harmony",
        "note": None,
    },
    "groq/compound*": {
        "requests_per_minute": 30,
        "requests_per_day": 250,
        "tokens_per_minute": 70_000,
        "tokens_per_day": None,
        # Absent from Groq's structured-outputs list, which is a positive list.
        # So this is a documented absence, not an open question: the family gets
        # json_object plus a schema check on this side, and a bounded retry.
        "output_mode": OutputMode.JSON_OBJECT,
        "tokenizer": None,
        "note": JSON_SCHEMA_UNSUPPORTED_BY_CURRENT_DOCS,
    },
}

#: Kept for callers that want the strict-capable family specifically.
PUBLISHED_FREE_LIMITS_GPT_OSS = PUBLISHED_FREE_LIMITS["openai/gpt-oss-*"]


class GroqError(ProviderFailure):
    """Every one of these carries the failure class the runner should record.

    Without it the runner would have to recognise these by name, and the only
    thing that decides whether an analysis is retried would be a string.
    """


class CredentialsMissing(GroqError):
    """No API key. The adapter exists and has never spoken to Groq."""

    failure_class = FailureClass.PROVIDER_NOT_CONFIGURED


class FreeQuotaExceeded(GroqError):
    """The request does not fit. The prompt is not the thing that gives way."""

    failure_class = FailureClass.QUOTA_BLOCKED


class QuotaUnknown(GroqError):
    """The account's limits have not been measured, so nothing may be sent.

    Separate from :class:`FreeQuotaExceeded` because the remedy is different:
    one is answered by measuring, the other by not using the free tier.
    """

    failure_class = FailureClass.QUOTA_BLOCKED


class InputPolicyViolation(GroqError):
    """Refusing to send the canonical method to a provider that may train on it."""

    failure_class = FailureClass.PRIVACY_POLICY_BLOCKED


class StructuredOutputError(GroqError):
    """The model returned something the contract cannot read."""

    failure_class = FailureClass.CONTRACT_VIOLATION


class RequestTooLarge(GroqError):
    """HTTP 413: this request, as it is, is not accepted on this account.

    Terminal: retrying sends the same request again. What the 413 means was
    measured rather than assumed (D-256). For ``openai/gpt-oss-120b`` the body
    says it: a single request whose token count exceeds the per-minute
    allowance. It follows tokens, not bytes - padding the body from 49 kB to
    299 kB changed nothing, and ASCII and Japanese text of the same token count
    were counted the same. ``groq/compound`` answers the same Entry request with
    a bare "Request Entity Too Large" naming no model and no limit, with its own
    70K-per-minute allowance untouched; Groq does not say why, and neither does
    this class.
    """

    failure_class = FailureClass.QUOTA_BLOCKED


class RateLimited(GroqError):
    """HTTP 429, after the transport's own retries. Worth trying again later."""

    failure_class = FailureClass.PROVIDER_RATE_LIMITED


class ProviderUnavailable(GroqError):
    """HTTP 5xx or a dropped connection. Worth trying again later."""

    failure_class = FailureClass.PROVIDER_ERROR


class RequestRejected(GroqError):
    """HTTP 400 or 422: the request itself is wrong, and will be wrong again."""

    failure_class = FailureClass.CONTRACT_VIOLATION


class CredentialsRejected(GroqError):
    """HTTP 401, 403 or 404: the key, its permissions or the model id."""

    failure_class = FailureClass.PROVIDER_NOT_CONFIGURED


def failure_for_status(status: int, detail: str) -> GroqError:
    """The failure an HTTP status is, so the runner retries only what can recover.

    Written out because the default would have been wrong: urllib's HTTPError is
    an OSError, and an OSError reads as the network - so a 413 or a 400 would
    have been retried as if the connection had dropped.
    """

    message = f"groq returned {status}: {detail}" if detail else f"groq returned {status}"
    if status == 413:
        return RequestTooLarge(message)
    if status == 429:
        return RateLimited(message)
    if status >= 500:
        return ProviderUnavailable(message)
    if status in (401, 403, 404):
        return CredentialsRejected(message)
    return RequestRejected(message)


def _groq_error_detail(exc) -> str:
    """Groq's own explanation from an error body: its type, code and message.

    Never the request, never its headers - the provider's description of what
    it refused, which is what makes a 400 or a 413 diagnosable.
    """

    try:
        body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
    except (ValueError, OSError, AttributeError):
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    detail = "; ".join(
        f"{field}={error[field]}" for field in ("type", "code", "message") if error.get(field)
    )
    # Groq names the organisation in rate-limit messages. Not a credential, but
    # an account identifier with no business in a log line or a probe record.
    return re.sub(r"org_[A-Za-z0-9]+", "org_<redacted>", detail)


class ExternalToolUsed(GroqError):
    """The model reached outside the bundle. Its answer cannot be used at all.

    Emphatically not retryable. Trying again means calling the provider again,
    which may reach outside the bundle again, and paying for it - for an answer
    that is disqualified either way.
    """

    failure_class = FailureClass.EXTERNAL_TOOL_USED


#: Reported when a provider may not receive production canonical traffic. Not a
#: quality problem and not a quota problem: an exposure the terms do not cover.
ANALYSIS_PRIVACY_GATE_BLOCKED = "ANALYSIS_PRIVACY_GATE_BLOCKED"

#: Reported when a response says a built-in tool ran. Not a quality problem - a
#: provenance one, and it disqualifies the output completely.
ANALYSIS_EXTERNAL_TOOL_USED = "ANALYSIS_EXTERNAL_TOOL_USED"

#: Groq's compound systems have these on by default: web search, visiting a
#: website, running code, and Wolfram Alpha. For this analysis that is not a
#: feature, it is a leak. A model that can search the web during a decision:
#:
#: * sees information from after ``decision_cutoff_at``, which is the one thing
#:   the entire availability model exists to prevent (CLAUDE.md 1-7, 1-16);
#: * produces teacher data contaminated by facts the system never had;
#: * bases a judgement on sources that are in no bundle and no hash, so the
#:   decision cannot be reproduced or audited (CLAUDE.md 1-18);
#: * spends money outside the zero-cost envelope.
#:
#: The request asks for them to be off. Groq does not currently *document* a way
#: to switch them off for compound, so that request is best effort and cannot be
#: relied on - which is why the response is checked, and the check is what
#: actually enforces this.
COMPOUND_MODEL_PREFIXES = ("groq/compound",)


def uses_built_in_tools(model_id: str) -> bool:
    return model_id.startswith(COMPOUND_MODEL_PREFIXES)


def executed_tools_in(payload: dict) -> list:
    """Any report of a tool having run, wherever the provider puts it.

    Looked for in three places on purpose. The field is documented on the
    response and its exact position is not something to be confident about, and
    the consequence of missing it is an answer built on unrecorded sources being
    treated as if it came from the bundle.
    """

    found: list = []
    for container in (payload, *(payload.get("choices") or [])):
        if not isinstance(container, dict):
            continue
        for key in ("executed_tools", "tool_calls"):
            value = container.get(key)
            if value:
                found.extend(value if isinstance(value, list) else [value])
        message = container.get("message")
        if isinstance(message, dict):
            for key in ("executed_tools", "tool_calls"):
                value = message.get(key)
                if value:
                    found.extend(value if isinstance(value, list) else [value])
    return found


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

    @property
    def privacy_gate_passes(self) -> bool:
        """Whether this provider may be used for production analysis.

        Stricter than :attr:`may_receive_the_canonical_method`, and deliberately.
        Not training on inputs is what makes an experiment acceptable; Zero Data
        Retention *confirmed switched on for this account* is what makes routine
        production traffic acceptable. "Available" is a fact about the product
        and says nothing about the account, so it does not pass.
        """

        return self.may_receive_the_canonical_method and self.zero_data_retention_enabled is True

    @property
    def privacy_gate_detail(self) -> str:
        if not self.may_receive_the_canonical_method:
            return f"inputs are {self.input_use.value}"
        if self.zero_data_retention_enabled is True:
            return "inputs are not trained on and ZDR is confirmed enabled on the account"
        if self.zero_data_retention_enabled is False:
            return "ZDR is available and is switched off for this account"
        return (
            "ZDR is available and nobody has confirmed it is switched on for this account. "
            "Whether it is on is a fact about the account, and this code has never seen the account"
        )

    def assert_production_privacy_gate_passes(self) -> None:
        """The gate for production traffic, which is stricter than the other one.

        Not training on inputs is what makes a one-off experiment acceptable.
        Routine production traffic is a different exposure: every request
        carries Canonical v5.1 in full, several times a day, for as long as the
        system runs. For that, Zero Data Retention has to be *confirmed switched
        on for this account* - not merely offered by the product.

        UNKNOWN is refused as firmly as FALSE. Whether ZDR is on is a fact about
        an account this code has never seen, and silence is not consent
        (CLAUDE.md, the quad-state licence vocabulary). Treating "nobody has
        checked" as a pass is exactly the reading that makes the check
        decorative.
        """

        if self.privacy_gate_passes:
            return
        raise InputPolicyViolation(
            f"{ANALYSIS_PRIVACY_GATE_BLOCKED}: {self.provider_id} may not receive production "
            f"canonical requests - {self.privacy_gate_detail}. The canonical prompt, the addenda "
            f"and the bundle are not sent. See {self.terms_url}"
        )

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
    # Not NONE_BY_DEFAULT. "Groq does not retain customer data for inference
    # requests" sits beside a documented possibility of transient retention for
    # reliability and abuse handling, and until Zero Data Retention is confirmed
    # switched on for the account, the weaker of the two is what is true.
    retention=Retention.TRANSIENT_FOR_ABUSE_AND_RELIABILITY,
    zero_data_retention_available=True,
    zero_data_retention_enabled=None,
    terms_url="https://console.groq.com/docs/legal/services-agreement",
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


def policy_from_env(env: dict[str, str] | None = None, *, today: date | None = None) -> DataPolicy:
    """GROQ_POLICY, with Zero Data Retention set from a recorded observation.

    GROQ_POLICY describes Groq's published terms, which are the same for every
    account, and so it has to leave ZDR as unknown: whether ZDR is on is a fact
    about one account. This is where that fact comes in - and it comes in as a
    dated observation of the console, because there is no API that reports it.

    A date in the future is refused rather than trusted. Nobody observed a
    setting tomorrow, and accepting one would make the attestation a formality.
    """

    source = env if env is not None else os.environ
    raw = (source.get(ENV_ZDR_CONFIRMED_ON) or "").strip()
    if not raw:
        return GROQ_POLICY
    try:
        observed = date.fromisoformat(raw)
    except ValueError as exc:
        raise GroqError(
            f"{ENV_ZDR_CONFIRMED_ON} has to be the ISO date the setting was observed, like 2026-09-18"
        ) from exc
    if observed > (today or date.today()):
        raise GroqError(
            f"{ENV_ZDR_CONFIRMED_ON} is {observed.isoformat()}, which has not happened yet; an "
            "observation cannot be dated in the future"
        )
    return replace(
        GROQ_POLICY,
        zero_data_retention_enabled=True,
        notes=(
            (GROQ_POLICY.notes or "")
            + f" Zero Data Retention observed enabled for this account in the console's Data "
            f"Controls on {observed.isoformat()}."
        ).strip(),
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
    requests_per_day: int | None = None
    #: Some accounts meter input and output separately (ITPM / OTPM) and some
    #: meter one combined figure. They are judged differently: a combined limit
    #: has to cover the prompt *and* the reserved completion, while separate
    #: limits are each compared with their own side.
    max_output_tokens_per_minute: int | None = None
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
    reserved_output_tokens: int
    quota: Quota
    fits: bool
    reason: str
    #: A runtime reason code, not a decision id. Present only when the request
    #: is refused because it does not fit a *measured* limit.
    reason_code: str | None = None

    @property
    def summary(self) -> dict:
        return {
            "prompt_tokens_estimated": self.prompt_tokens_estimated,
            "reserved_output_tokens": self.reserved_output_tokens,
            "fits": self.fits,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "quota_source": self.quota.source,
        }


def preflight(
    prompt: str,
    *,
    quota: Quota,
    requests_per_day: int = 1,
    reserved_output_tokens: int = DEFAULT_COMPLETION_RESERVE,
) -> Preflight:
    """Decide whether this request may be sent, without changing the prompt.

    There is no ``max_chars`` and no truncation argument, by design. If the
    bundle does not fit, the finding is that the tier cannot run this analysis -
    which is worth having - and not that v5.1 needs shortening.

    The completion reserve counts. Where an account meters one combined
    tokens-per-minute figure, the model's own output is spent from the same
    allowance as the prompt, so a preflight that weighed only the input would
    approve requests the API then refuses. Where input and output are metered
    separately, each is compared with its own limit instead.
    """

    estimated = estimate_tokens(prompt)
    combined = estimated + max(0, reserved_output_tokens)

    if not quota.is_known:
        return Preflight(
            prompt_tokens_estimated=estimated,
            reserved_output_tokens=reserved_output_tokens,
            quota=quota,
            fits=False,
            reason=(
                f"the account's limits have not been measured, so whether ~{estimated} input "
                f"tokens plus a {reserved_output_tokens} token completion reserve fit is unknown. "
                "Read them from the rate-limit headers of one real request before running a batch"
            ),
            reason_code=None,
        )

    per_request = quota.max_input_tokens_per_request
    if per_request is not None and estimated > per_request:
        return Preflight(
            prompt_tokens_estimated=estimated,
            reserved_output_tokens=reserved_output_tokens,
            quota=quota,
            fits=False,
            reason=(
                f"~{estimated} input tokens against a per-request limit of {per_request}. The "
                "canonical prompt is not shortened to fit a quota: every answer would then be an "
                "answer to a different method, and nothing in the output would show it"
            ),
            reason_code=ANALYSIS_FREE_QUOTA_BLOCKED,
        )

    # Separate input and output meters, judged separately.
    if quota.max_input_tokens_per_minute is not None:
        if estimated > quota.max_input_tokens_per_minute:
            return Preflight(
                prompt_tokens_estimated=estimated,
                reserved_output_tokens=reserved_output_tokens,
                quota=quota,
                fits=False,
                reason=(
                    f"~{estimated} input tokens exceeds the whole input allowance per minute "
                    f"({quota.max_input_tokens_per_minute}); a single request cannot be sent"
                ),
                reason_code=ANALYSIS_FREE_QUOTA_BLOCKED,
            )
        if (
            quota.max_output_tokens_per_minute is not None
            and reserved_output_tokens > quota.max_output_tokens_per_minute
        ):
            return Preflight(
                prompt_tokens_estimated=estimated,
                reserved_output_tokens=reserved_output_tokens,
                quota=quota,
                fits=False,
                reason=(
                    f"a {reserved_output_tokens} token completion reserve exceeds the output "
                    f"allowance per minute ({quota.max_output_tokens_per_minute})"
                ),
                reason_code=ANALYSIS_FREE_QUOTA_BLOCKED,
            )
        return Preflight(
            prompt_tokens_estimated=estimated,
            reserved_output_tokens=reserved_output_tokens,
            quota=quota,
            fits=True,
            reason=(
                f"~{estimated} input tokens and a {reserved_output_tokens} token reserve are "
                "inside the separately measured input and output limits"
            ),
        )

    # One combined meter: the prompt and the reserve share it.
    per_minute = quota.max_tokens_per_minute
    if per_minute is not None and estimated > per_minute:
        # Certain: the prompt alone is over, whatever the completion. Measured
        # (D-256): Groq refuses such a request with HTTP 413 and a message
        # naming the per-minute limit, and counts tokens, not bytes.
        return Preflight(
            prompt_tokens_estimated=estimated,
            reserved_output_tokens=reserved_output_tokens,
            quota=quota,
            fits=False,
            reason=(
                f"~{estimated} input tokens alone exceed the combined allowance of {per_minute} "
                "per minute. A single request cannot be sent at all"
            ),
            reason_code=ANALYSIS_FREE_QUOTA_BLOCKED,
        )
    if per_minute is not None and combined > per_minute:
        # Not certain: Groq was measured to count the completion as less than
        # max_completion_tokens, so it may accept the request. Treated as not
        # fitting anyway - a request that uses the whole minute leaves nothing
        # to pace a batch with - and the contract smoke is what decides.
        return Preflight(
            prompt_tokens_estimated=estimated,
            reserved_output_tokens=reserved_output_tokens,
            quota=quota,
            fits=False,
            reason=(
                f"~{estimated} input tokens plus a {reserved_output_tokens} token completion "
                f"reserve is {combined}, over the combined allowance of {per_minute} per minute. "
                "Groq counts less than the full reserve, so it may accept the request, but it "
                "would leave no room to pace a batch; the contract smoke decides"
            ),
            reason_code=ANALYSIS_FREE_QUOTA_BLOCKED,
        )

    if per_minute is not None and requests_per_day > 1:
        minutes = (combined * requests_per_day) / per_minute
        return Preflight(
            prompt_tokens_estimated=estimated,
            reserved_output_tokens=reserved_output_tokens,
            quota=quota,
            fits=True,
            reason=(
                f"~{combined} tokens each including the reserve; {requests_per_day} of them need "
                f"about {minutes:.1f} minute(s) of the per-minute allowance, so the batch has to "
                "be paced"
            ),
        )

    return Preflight(
        prompt_tokens_estimated=estimated,
        reserved_output_tokens=reserved_output_tokens,
        quota=quota,
        fits=True,
        reason=f"~{combined} tokens including the reserve is inside the measured limits",
    )


#: The headers Groq documents. ``x-ratelimit-limit-requests`` is requests per
#: DAY and ``x-ratelimit-limit-tokens`` is tokens per MINUTE - an asymmetry worth
#: naming, because reading either as "per request window" would be wrong by
#: three orders of magnitude in opposite directions.
RATE_LIMIT_HEADERS = {
    "requests_per_day": "x-ratelimit-limit-requests",
    "tokens_per_minute": "x-ratelimit-limit-tokens",
    "remaining_requests": "x-ratelimit-remaining-requests",
    "remaining_tokens": "x-ratelimit-remaining-tokens",
    "reset_requests": "x-ratelimit-reset-requests",
    "reset_tokens": "x-ratelimit-reset-tokens",
    # Not in Groq's documented set today. Read anyway: an account that meters
    # input and output separately reports them under these names, and the two
    # are judged differently from one combined figure.
    "input_tokens_per_minute": "x-ratelimit-limit-input-tokens",
    "output_tokens_per_minute": "x-ratelimit-limit-output-tokens",
}

#: What the probe sends. No canonical prompt, no addenda, no security, no price,
#: no material, and no response format. The point is to learn the account's
#: limits before anything that matters is transmitted, so the probe must not be
#: the thing that transmits it - and it must not depend on a structured-output
#: capability it is partly being run to discover.
PROBE_PROMPT = "ok"


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def quota_from_headers(headers: dict, *, source: str) -> Quota:
    """Read an account's real limits out of one response's headers."""

    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    return Quota(
        max_input_tokens_per_request=None,
        max_tokens_per_minute=_int_or_none(lowered.get(RATE_LIMIT_HEADERS["tokens_per_minute"])),
        max_input_tokens_per_minute=_int_or_none(
            lowered.get(RATE_LIMIT_HEADERS["input_tokens_per_minute"])
        ),
        max_output_tokens_per_minute=_int_or_none(
            lowered.get(RATE_LIMIT_HEADERS["output_tokens_per_minute"])
        ),
        max_requests_per_minute=None,
        requests_per_day=_int_or_none(lowered.get(RATE_LIMIT_HEADERS["requests_per_day"])),
        source=source,
    )


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


def json_object_format() -> dict:
    """For a model the vendor does not document for json_schema.

    Valid JSON is all this asks for. The shape is then checked here, which is
    where it has to be checked anyway: a schema honoured by the provider is a
    convenience, and the contract is enforced on this side regardless.
    """

    return {"type": "json_object"}


def schema_violations(payload: dict, schema: dict) -> list[str]:
    """The subset of JSON Schema this contract actually uses, checked locally.

    Deliberately not a general validator. It covers required keys, null unions,
    enums and arrays of enums, because that is what :func:`stage3_schema` and
    :func:`entry_analysis_schema` are made of - and a partial check that is
    obviously partial is safer than a dependency that looks total.
    """

    problems: list[str] = []
    properties = schema.get("properties", {})

    for key in schema.get("required", []):
        if key not in payload:
            problems.append(f"missing required field {key!r}")

    if not schema.get("additionalProperties", True):
        for key in payload:
            if key not in properties and not key.startswith("_"):
                problems.append(f"unexpected field {key!r}")

    for key, spec in properties.items():
        if key not in payload:
            continue
        value = payload[key]
        allowed = spec.get("type")
        types = [allowed] if isinstance(allowed, str) else list(allowed or [])
        if value is None:
            if types and "null" not in types:
                problems.append(f"{key!r} is null and null is not permitted")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{key!r} is {value!r}, which is not one of {spec['enum']}")
        if types and "array" in types or spec.get("type") == "array":
            if not isinstance(value, list):
                problems.append(f"{key!r} should be an array")
            else:
                member = (spec.get("items") or {}).get("enum")
                if member:
                    for entry in value:
                        if entry not in member:
                            problems.append(f"{key!r} contains {entry!r}, not one of {member}")
        elif types and "number" in types and not isinstance(value, int | float):
            problems.append(f"{key!r} should be a number, got {type(value).__name__}")
        elif types and "boolean" in types and not isinstance(value, bool):
            problems.append(f"{key!r} should be a boolean, got {type(value).__name__}")
        elif types and "string" in types and not isinstance(value, str):
            problems.append(f"{key!r} should be a string, got {type(value).__name__}")

    return problems


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
    #: Overrides the derived mode when a family is known to accept a best-effort
    #: schema. Left unset, an undocumented model gets json_object.
    forced_output_mode: OutputMode | None = None
    #: PRODUCTION by default, because the default has to be the strict one. A
    #: smoke that forgot to say so would otherwise send the canonical method
    #: under the weaker gate, which is the failure the two modes exist to
    #: separate.
    mode: RequestMode = RequestMode.PRODUCTION
    max_json_object_attempts: int = MAX_JSON_OBJECT_ATTEMPTS
    last_usage: GroqUsage | None = field(default=None, repr=False)
    last_preflight: Preflight | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **overrides) -> GroqHostedProvider:
        source = env if env is not None else os.environ
        key = (source.get(ENV_API_KEY) or "").strip()
        model = (source.get(ENV_MODEL) or "").strip()
        if not key:
            raise CredentialsMissing(
                f"{ENV_API_KEY} is not set. On this machine it comes from the encrypted store: "
                f"ops\\windows\\Set-SurgeSecret.ps1 -Name {ENV_API_KEY}, then run the job through "
                "ops\\windows\\Invoke-WithSurgeSecrets.ps1. Never in the repository, never in a "
                "chat message. Until then the analysis provider is the deterministic stand-in, "
                "which cannot produce a formal prediction"
            )
        if not model:
            raise GroqError(
                f"{ENV_MODEL} is not set. The model is configuration, not code: name the one this "
                "run should use so the output row records which model answered"
            )
        overrides.setdefault("policy", policy_from_env(source))
        return cls(
            model_id=model,
            api_key=key,
            strict_model_ids=strict_models(source),
            **overrides,
        )

    @property
    def supports_strict(self) -> bool:
        return self.model_id in self.strict_model_ids

    @property
    def output_mode(self) -> OutputMode:
        """What this model may be asked for, from what its vendor documents.

        A model absent from the strict list is not thereby assumed to honour a
        best-effort schema either. The safe reading of a positive list is that
        anything not on it gets json_object plus a local schema check, which
        works for both and costs a retry loop.
        """

        if self.supports_strict:
            return OutputMode.STRICT_JSON_SCHEMA
        return self.forced_output_mode or OutputMode.JSON_OBJECT

    # ------------------------------------------------------------- requests

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

    @staticmethod
    def json_object_framing(schema: dict) -> str:
        """The system message a JSON_OBJECT request has to carry.

        Groq refuses a ``json_object`` request unless the messages contain the
        word "json" - found live, as HTTP 400 "'messages' must contain the word
        'json' in some form, to use 'response_format' of type 'json_object'".
        The rendered Entry prompt never says it, so every compound request would
        have been refused; the unit tests could not see it, because their fake
        transport did not enforce a rule nobody had written down.

        It is a separate system message rather than an edit to the rendered
        prompt, so the prompt - and the prompt_sha256 recorded at TX1 - is the
        same whichever output mode carries it. And it states the schema, because
        json_object mode enforces only that the answer is JSON, not which JSON:
        the model has to be told the shape it will be checked against.
        """

        return (
            "Respond with exactly one JSON object and nothing else. It is checked against "
            "this JSON Schema, and an answer that does not conform is discarded:\n"
            + json.dumps(schema, sort_keys=True, separators=(",", ":"))
        )

    def _request_body(self, prompt: str, *, schema_name: str, schema: dict, mode: OutputMode) -> bytes:
        if mode.sends_a_schema:
            fmt = response_format(
                schema_name, schema, strict=mode is OutputMode.STRICT_JSON_SCHEMA
            )
        elif mode is OutputMode.JSON_OBJECT:
            fmt = json_object_format()
        else:
            fmt = None

        messages = [{"role": "user", "content": prompt}]
        if mode is OutputMode.JSON_OBJECT:
            messages.insert(0, {"role": "system", "content": self.json_object_framing(schema)})
        payload = {
            "model": self.model_id,
            "messages": messages,
            "temperature": self.temperature,
            "max_completion_tokens": self.max_completion_tokens,
        }
        if fmt is not None:
            payload["response_format"] = fmt
        if uses_built_in_tools(self.model_id):
            # Two guards, and they are not the same strength.
            #
            # `tool_choice` is documented in the Groq API reference: "none means
            # the model will not call any tool and instead generates a message",
            # and `disable_tool_validation` says "tool_choice=required/none will
            # still be enforced". So this is a request the API undertakes to
            # honour, not a hint, and it is required here rather than optional.
            #
            # `compound_custom.tools.enabled_tools` is documented too - "a list
            # of tool names that are enabled for the request" - but what an
            # *empty* list means is not stated anywhere. It could plausibly read
            # as "none" or as "unset, use the defaults", and the defaults are
            # every built-in tool switched on. So it is sent as defence in depth
            # and nothing depends on it alone.
            #
            # The response check is the third layer and the only unconditional
            # one: whatever the request said, an answer that used a tool is
            # refused.
            payload["tool_choice"] = "none"
            payload["compound_custom"] = {"tools": {"enabled_tools": []}}
        # UTF-8, not ASCII escapes. The canonical method is mostly Japanese, and
        # json.dumps' default turns every one of those characters into a
        # six-byte \uXXXX escape: measured, the same Entry request is 80,422
        # bytes escaped and 49,407 as UTF-8, with identical content.
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _post(self, body: bytes):
        import urllib.error

        try:
            response = self.transport(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                data=body,
                method="POST",
            )
        except urllib.error.HTTPError as exc:
            raise failure_for_status(exc.code, _groq_error_detail(exc)) from exc
        except RuntimeError as exc:
            # The transport retries 429 and 5xx itself and then gives up with a
            # RuntimeError chained to the last error, which is what says which.
            cause = exc.__cause__
            if isinstance(cause, urllib.error.HTTPError):
                raise failure_for_status(cause.code, _groq_error_detail(cause)) from exc
            raise ProviderUnavailable(str(exc)) from exc
        if response.status != 200:
            raise failure_for_status(response.status, "")
        return response

    def _content_of(self, payload: dict) -> str:
        used = executed_tools_in(payload)
        if used:
            names = [
                (tool.get("type") or tool.get("name") or "?") if isinstance(tool, dict) else str(tool)
                for tool in used
            ]
            raise ExternalToolUsed(
                f"{ANALYSIS_EXTERNAL_TOOL_USED}: the model ran {len(used)} built-in tool(s) "
                f"({', '.join(sorted(set(names)))}). The answer rests on sources that are in no "
                "bundle and no hash, and may postdate the decision cutoff, so it cannot be used "
                "for a Stage 3 setup, an entry decision or teacher data. The analysis fails"
            )
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
        return content

    def _call(self, prompt: str, *, schema_name: str, schema: dict) -> dict:
        # The production gate, not the weaker one. This is the path every
        # canonical request takes, and it carries the method in full: the
        # requirement is not just "will not train on it" but "will not retain
        # it, confirmed for this account".
        #
        # RESEARCH_SMOKE is the deliberate exception and it is a mode, not a
        # relaxation - it sends a fixed harmless prompt, never the canonical
        # method, so the gate it needs is the weaker one.
        if self.mode is RequestMode.RESEARCH_SMOKE:
            self.policy.assert_may_receive_the_canonical_method()
        else:
            self.policy.assert_production_privacy_gate_passes()

        # Measured on everything that goes out, not only the prompt: in
        # JSON_OBJECT mode the schema travels as a system message too, and a
        # preflight that left it out would pass a request the account refuses.
        sent = prompt
        if self.output_mode is OutputMode.JSON_OBJECT:
            sent = self.json_object_framing(schema) + "\n" + prompt
        check = preflight(
            sent, quota=self.quota, reserved_output_tokens=self.max_completion_tokens
        )
        self.last_preflight = check
        if not check.fits and check.reason_code == ANALYSIS_FREE_QUOTA_BLOCKED:
            raise FreeQuotaExceeded(f"{check.reason_code}: {check.reason}")
        if not check.fits:
            # The hole this closes: an unmeasured quota produced fits=False with
            # no reason code, and the send went ahead anyway. The canonical
            # method would have gone out before anyone knew whether it could.
            raise QuotaUnknown(
                f"{check.reason} Run quota_probe() first: it measures the account's limits with a "
                "request that carries no canonical prompt, no addenda and no real market data."
            )

        mode = self.output_mode
        attempts = self.max_json_object_attempts if mode.needs_client_side_retry else 1
        body = self._request_body(prompt, schema_name=schema_name, schema=schema, mode=mode)
        last: str | None = None

        for attempt in range(1, attempts + 1):
            response = self._post(body)
            payload = json.loads(response.body.decode("utf-8"))
            self.last_usage = GroqUsage.from_payload(payload)
            content = self._content_of(payload)

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                last = f"the content was not JSON: {exc}"
                if attempt < attempts:
                    continue
                raise StructuredOutputError(last) from exc

            if not isinstance(parsed, dict):
                last = f"the content was JSON but not an object ({type(parsed).__name__})"
                if attempt < attempts:
                    continue
                raise StructuredOutputError(last)

            # Checked here whatever the mode. A schema the provider enforced is
            # a convenience; the contract is this system's, so it is verified on
            # this side even when the vendor promised to honour it.
            problems = schema_violations(parsed, schema)
            if problems:
                last = "the answer did not match the contract: " + "; ".join(problems)
                if attempt < attempts:
                    continue
                raise StructuredOutputError(
                    f"{last} (after {attempts} attempt(s); the analysis fails rather than "
                    "proceeding to a decision on a malformed answer)"
                )

            parsed["_raw"] = content
            parsed["_attempts"] = attempt
            parsed["_output_mode"] = mode.value
            return parsed

        raise StructuredOutputError(last or "no answer")  # pragma: no cover - loop always returns

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

    # ----------------------------------------------------------- the probe

    def quota_probe(self) -> Quota:
        """Learn this account's limits, before sending anything that matters.

        One request, carrying a fixed two-word prompt and a two-field schema. No
        canonical prompt, no addenda, no security, no price, no material - the
        whole point is to find out what the account allows *before* the method
        is transmitted, so a probe that carried the method would defeat itself.

        The measured quota replaces whatever was configured, and until it has
        run, :meth:`analyse` and :meth:`analyse_entry` refuse to send.
        """

        self.policy.assert_may_receive_the_canonical_method()

        # No response_format at all. The probe exists to read headers, and
        # asking for structured output would make it depend on the very
        # capability it is being run to find out about - a probe that fails on
        # an undocumented model teaches nothing about that model's limits.
        probe = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "temperature": 0.0,
            "max_completion_tokens": 16,
        }
        if uses_built_in_tools(self.model_id):
            # A probe has no reason to search the web either. Its answer is
            # discarded, but a tool call would cost money, leave a trace at a
            # third party and make the measured token count describe a request
            # nothing else will ever send.
            probe["tool_choice"] = "none"
            probe["compound_custom"] = {"tools": {"enabled_tools": []}}
        body = json.dumps(probe).encode("utf-8")

        response = self.transport(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            data=body,
            method="POST",
            accept_statuses=(429,),
        )
        headers = getattr(response, "headers", {}) or {}
        measured = quota_from_headers(
            headers,
            source=(
                f"measured from this account's rate-limit headers on a probe request "
                f"(HTTP {response.status})"
            ),
        )
        if not measured.is_known:
            raise QuotaUnknown(
                "the probe returned no rate-limit headers, so the account's limits are still "
                "unknown. Nothing carrying the canonical method will be sent until they are"
            )
        self.quota = measured
        return measured

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
                    "output_mode": self.output_mode.value,
                    "recorded_at": datetime.now(UTC).date().isoformat(),
                }
            ),
        }


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


__all__ = [
    "ANALYSIS_EXTERNAL_TOOL_USED",
    "ANALYSIS_PRIVACY_GATE_BLOCKED",
    "ANALYSIS_FREE_QUOTA_BLOCKED",
    "COMPOUND_MODEL_PREFIXES",
    "CredentialsRejected",
    "ExternalToolUsed",
    "ProviderUnavailable",
    "RateLimited",
    "RequestRejected",
    "RequestTooLarge",
    "failure_for_status",
    "executed_tools_in",
    "uses_built_in_tools",
    "PUBLISHED_FREE_LIMITS",
    "PUBLISHED_FREE_LIMITS_GPT_OSS",
    "RATE_LIMIT_HEADERS",
    "QuotaUnknown",
    "quota_from_headers",
    "DOCUMENTED_STRICT_MODELS",
    "ENV_API_KEY",
    "ENV_MODEL",
    "GEMINI_FREE_TIER_POLICY",
    "GROQ_POLICY",
    "policy_from_env",
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
    "DEFAULT_COMPLETION_RESERVE",
    "JSON_SCHEMA_UNSUPPORTED_BY_CURRENT_DOCS",
    "MAX_JSON_OBJECT_ATTEMPTS",
    "OutputMode",
    "RequestMode",
    "entry_analysis_schema",
    "json_object_format",
    "schema_violations",
    "estimate_tokens",
    "preflight",
    "response_format",
    "stage3_schema",
    "strict_models",
]
