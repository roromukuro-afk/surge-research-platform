"""Phase 10: the teacher dataset.

The objective layer measures; the interpretive layer judges. A model is trained
on the second, through a versioned admission policy, and never on the first -
"rose 20% within twenty sessions" is trivially computable, looks like ground
truth, and teaches a model to predict price moves rather than to predict this
system's decisions being right.

Status: IMPLEMENTED_NOT_LIVE_VERIFIED. There are no real episodes yet, so no
labels have been produced from live outcomes.
"""

from surge.labels.admission import AdmissionPolicy, DatasetManifest, build
from surge.labels.misses import EvidenceDocument, MissVerdict, classify
from surge.labels.models import (
    InterpretiveJudgement,
    InterpretiveLabel,
    LabelError,
    LabelSet,
    ObjectiveLabel,
    ObservationKind,
    ReviewStatus,
)
from surge.labels.objective import for_a_security_never_entered, from_outcome

__all__ = [
    "AdmissionPolicy",
    "DatasetManifest",
    "EvidenceDocument",
    "InterpretiveJudgement",
    "InterpretiveLabel",
    "LabelError",
    "LabelSet",
    "MissVerdict",
    "ObjectiveLabel",
    "ObservationKind",
    "ReviewStatus",
    "build",
    "classify",
    "for_a_security_never_entered",
    "from_outcome",
]
