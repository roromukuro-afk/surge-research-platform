"""Phase 7: Stage 3 end-of-day analysis.

The pipeline runs against a deterministic stand-in by default, so no paid model
subscription is needed to exercise it end to end. Connecting a real model changes
one provider, not the bundle, the validator or the storage.
"""

from surge.analysis.bundle import (
    BUNDLE_VERSION,
    SECTION_ORDER,
    BundleError,
    InputBundle,
    build_bundle,
    canonical_json,
    missing_sections,
    sha256_text,
)
from surge.analysis.llm import (
    MOCK_PROVIDER_VERSION,
    DeterministicMockProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderKind,
    Stage3State,
    ZoneBasisKind,
    render_prompt,
)
from surge.analysis.stage3 import STAGE3_JOB_VERSION, Stage3Job, Stage3Report, Stage3Result
from surge.analysis.validate import (
    VALIDATOR_VERSION,
    ValidationResult,
    ValidationStatus,
    apply_repairs,
    twenty_percent_threshold,
    validate,
)

__all__ = [
    "BUNDLE_VERSION",
    "MOCK_PROVIDER_VERSION",
    "SECTION_ORDER",
    "STAGE3_JOB_VERSION",
    "VALIDATOR_VERSION",
    "BundleError",
    "DeterministicMockProvider",
    "InputBundle",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "ProviderKind",
    "Stage3Job",
    "Stage3Report",
    "Stage3Result",
    "Stage3State",
    "ValidationResult",
    "ValidationStatus",
    "ZoneBasisKind",
    "apply_repairs",
    "build_bundle",
    "canonical_json",
    "missing_sections",
    "render_prompt",
    "sha256_text",
    "twenty_percent_threshold",
    "validate",
]
