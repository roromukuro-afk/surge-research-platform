"""Stage 3, end to end: bundle, prompt, provider, validate, record.

The job takes its inputs rather than fetching them, for the same reason the
screening jobs do: a step that reaches out to storage mid-analysis has a
knowledge cutoff nobody can state, and the whole point of this phase is that the
cutoff is an argument.

What comes out is a row and a verdict, never a prediction. The output states stop
at setup and watch; entry belongs to Phase 8 and to a live price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from surge.analysis.bundle import BUNDLE_VERSION, InputBundle, build_bundle, missing_sections
from surge.analysis.llm import (
    DeterministicMockProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Stage3State,
    render_prompt,
)
from surge.analysis.validate import (
    VALIDATOR_VERSION,
    ValidationResult,
    ValidationStatus,
    apply_repairs,
    twenty_percent_threshold,
    validate,
)

STAGE3_JOB_VERSION = "stage3-analysis-1.0.0"


@dataclass
class Stage3Result:
    """One security's analysis, ready to be written."""

    security_id: str
    as_of_date: date
    bundle: InputBundle
    response: LLMResponse
    validation: ValidationResult
    prompt_sha256: str
    knowledge_cutoff: datetime
    threshold_reference_price: float | None = None
    threshold_reference_kind: str | None = None

    @property
    def state(self) -> Stage3State:
        return self.response.state

    @property
    def twenty_percent_threshold_price(self) -> float | None:
        return twenty_percent_threshold(self.threshold_reference_price)

    def as_row(self, *, run_id: str, bundle_id: str, available_at: datetime) -> dict:
        response = self.response
        return {
            "bundle_id": bundle_id,
            "run_id": run_id,
            "security_id": self.security_id,
            "as_of_date": self.as_of_date,
            "state": response.state.value,
            "rationale": response.rationale,
            "confidence_note": response.confidence_note,
            "twenty_percent_threshold_price": self.twenty_percent_threshold_price,
            "threshold_reference_price": self.threshold_reference_price,
            "threshold_reference_kind": self.threshold_reference_kind,
            "reachable_zone_low": response.reachable_zone_low,
            "reachable_zone_high": response.reachable_zone_high,
            "reachable_zone_basis_kinds": [kind.value for kind in response.reachable_zone_basis_kinds],
            "reachable_zone_basis": response.reachable_zone_basis,
            "concepts_considered": list(response.concepts_considered),
            "provider_kind": response.provider_kind.value,
            "provider_id": response.provider_id,
            "model_id": response.model_id,
            "prompt_sha256": self.prompt_sha256,
            "response_sha256": response.response_sha256,
            "validation_status": self.validation.status.value,
            "validation_errors": list(self.validation.errors),
            "knowledge_cutoff": self.knowledge_cutoff,
            "available_at": available_at,
        }


@dataclass
class Stage3Report:
    as_of_date: date
    job_version: str = STAGE3_JOB_VERSION
    validator_version: str = VALIDATOR_VERSION
    bundle_version: str = BUNDLE_VERSION
    results: list[Stage3Result] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        counts = {state.value: 0 for state in Stage3State}
        for result in self.results:
            counts[result.state.value] += 1
        counts["rejected_by_validator"] = sum(
            1 for result in self.results if result.validation.status is ValidationStatus.REJECTED
        )
        counts["repaired"] = sum(
            1 for result in self.results if result.validation.status is ValidationStatus.REPAIRED
        )
        counts["skipped"] = len(self.skipped)
        return counts

    @property
    def stored(self) -> list[Stage3Result]:
        """Only the answers that survived validation.

        A rejected answer is kept in the report and not written as an analysis.
        Storing it as one would put a verdict the validator refused next to the
        verdicts it accepted, with only a status column between them.
        """

        return [r for r in self.results if r.validation.passed]


class Stage3Job:
    """Runs Stage 3 over a set of candidates for one date."""

    job_name = "stage3_analysis"
    job_version = STAGE3_JOB_VERSION

    def __init__(self, *, provider: LLMProvider | None = None) -> None:
        # The default costs nothing and needs no credential, so the pipeline is
        # runnable by anyone who checks out the repository.
        self._provider = provider or DeterministicMockProvider()

    def run(
        self,
        *,
        as_of_date: date,
        knowledge_cutoff: datetime,
        canonical_text: str,
        canonical_prompt_sha256: str,
        candidates,
        addenda_texts=(),
        addenda_sha256=(),
        versions=None,
    ) -> Stage3Report:
        report = Stage3Report(as_of_date=as_of_date)

        for candidate in candidates:
            security_id = candidate["security_id"]
            bundle = build_bundle(
                security_id=security_id,
                market_code=candidate["market_code"],
                as_of_date=as_of_date,
                knowledge_cutoff=knowledge_cutoff,
                canonical_prompt_sha256=canonical_prompt_sha256,
                addenda_sha256=addenda_sha256,
                security=candidate.get("security"),
                market_data=candidate.get("market_data"),
                features=candidate.get("features"),
                routes=candidate.get("routes"),
                materials=candidate.get("materials"),
                entity_links=candidate.get("entity_links"),
                price_obstacles=candidate.get("price_obstacles"),
                stage2=candidate.get("stage2"),
                coverage=candidate.get("coverage"),
                versions=versions,
            )

            prompt = render_prompt(bundle, canonical_text, addenda_texts)
            request = LLMRequest(prompt=prompt, bundle=bundle)

            try:
                response = self._provider.analyse(request)
            except Exception as exc:  # noqa: BLE001 - one security must not stop the day
                report.skipped.append((security_id, f"{type(exc).__name__}: {exc}"))
                continue

            stage2 = bundle.sections.get("stage2") or {}
            reference_price = candidate.get("threshold_reference_price")
            validation = validate(
                response,
                close=stage2.get("close"),
                threshold_price=twenty_percent_threshold(reference_price),
                obstacles=bundle.sections.get("price_obstacles") or (),
                missing_sections=missing_sections(bundle),
            )
            response = apply_repairs(response, validation)

            report.results.append(
                Stage3Result(
                    security_id=security_id,
                    as_of_date=as_of_date,
                    bundle=bundle,
                    response=response,
                    validation=validation,
                    prompt_sha256=request.prompt_sha256,
                    knowledge_cutoff=knowledge_cutoff,
                    threshold_reference_price=reference_price,
                    threshold_reference_kind=candidate.get("threshold_reference_kind"),
                )
            )

        return report
