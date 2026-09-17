"""Phase 5 vocabulary: events, who reported them, and how they reach a security.

Three things are kept apart here on purpose, because collapsing any of them
changes what the platform is able to conclude later:

**Event, source, relation.** Five articles about one earnings revision are one
event with five sources, not five materials. One event can touch a dozen
securities through different mechanisms, and each of those is a separate claim.

**Discovery and verification.** Which role a source played is a property of the
link for that event, not of the publisher. There is deliberately no ranking that
puts investor relations above news: strength is measured from the event's own
features, not from the letterhead it arrived on.

**The seven features.** novelty, surprise, directness, magnitude, persistence,
market reaction and priced-in stay seven numbers. Blending them into one score
would delete the evidence needed to find out which of them predicts anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from surge.news.models import TimePrecision


class RelationType(StrEnum):
    """How an event reaches a security.

    Order matters: the members are listed most direct first, and
    :func:`strongest` relies on that ordering.
    """

    DIRECT_COMPANY = "DIRECT_COMPANY"
    SUBSIDIARY = "SUBSIDIARY"
    PRODUCT = "PRODUCT"
    CUSTOMER = "CUSTOMER"
    SUPPLIER = "SUPPLIER"
    COMPETITOR = "COMPETITOR"
    INDUSTRY = "INDUSTRY"
    POLICY_EXPOSURE = "POLICY_EXPOSURE"
    COMMODITY_EXPOSURE = "COMMODITY_EXPOSURE"
    FX_EXPOSURE = "FX_EXPOSURE"
    RATE_EXPOSURE = "RATE_EXPOSURE"
    GEOPOLITICAL_EXPOSURE = "GEOPOLITICAL_EXPOSURE"
    WEAK_ASSOCIATION = "WEAK_ASSOCIATION"


#: Relation types whose claim is "the event happened to this company or its
#: immediate commercial surface".
DIRECT_RELATIONS: frozenset[RelationType] = frozenset(
    {RelationType.DIRECT_COMPANY, RelationType.SUBSIDIARY, RelationType.PRODUCT}
)

#: Relation types that assert a transmission mechanism rather than an event about
#: the company. Each of these requires a written causal path - the database
#: enforces it too, but failing early with a readable message is kinder.
MACRO_RELATIONS: frozenset[RelationType] = frozenset(
    {
        RelationType.INDUSTRY,
        RelationType.POLICY_EXPOSURE,
        RelationType.COMMODITY_EXPOSURE,
        RelationType.FX_EXPOSURE,
        RelationType.RATE_EXPOSURE,
        RelationType.GEOPOLITICAL_EXPOSURE,
    }
)

_ORDER = {relation: index for index, relation in enumerate(RelationType)}


def strongest(relations) -> RelationType | None:
    """The most direct relation in a collection, or None if it is empty.

    "Most direct" is about the length of the inference chain, not about how
    important the event is. A supplier bankruptcy can matter far more than a
    routine filing; directness only says how many steps of reasoning stand
    between the document and the security.
    """

    chosen = [r for r in relations if r is not None]
    if not chosen:
        return None
    return min(chosen, key=lambda relation: _ORDER[relation])


class SourceRole(StrEnum):
    DISCOVERY = "DISCOVERY"
    VERIFICATION = "VERIFICATION"
    CORROBORATION = "CORROBORATION"


class MarketRelevance(StrEnum):
    RELEVANT = "RELEVANT"
    NOT_RELEVANT = "NOT_RELEVANT"
    UNCERTAIN = "UNCERTAIN"


class SessionTiming(StrEnum):
    """Whether a material landed before or after the session closed.

    Three values, not two. Deciding between a chart setup and a catalyst setup
    turns on this, and without a verified trading calendar the honest answer is
    that we do not know - so ``UNKNOWN`` exists and neither downstream state may
    be asserted from it. Defaulting an unknown to PRE_CLOSE would quietly claim
    the close had priced something it may never have seen.
    """

    PRE_CLOSE = "PRE_CLOSE"
    POST_CLOSE = "POST_CLOSE"
    UNKNOWN = "UNKNOWN"


class LinkConfidence(StrEnum):
    """How a link was established, not how strongly anyone feels about it."""

    STRONG = "STRONG"
    REGISTRY_ANCHORED = "REGISTRY_ANCHORED"
    PROVISIONAL = "PROVISIONAL"


class MaterialError(ValueError):
    """Raised when a claim is malformed rather than merely weak."""


@dataclass(frozen=True)
class EventSource:
    """One document's contribution to one event."""

    document_id: str
    source_key: str
    role: SourceRole
    available_to_model_at: datetime
    match_evidence: dict | None = None


@dataclass(frozen=True)
class MaterialEvent:
    """One real-world event, however many documents reported it."""

    event_key: str
    event_type: str
    scope: str
    merge_version: str
    sources: tuple[EventSource, ...]
    headline: str | None = None
    occurred_at: datetime | None = None
    occurred_at_precision: TimePrecision = TimePrecision.UNKNOWN
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.sources:
            raise MaterialError(f"event {self.event_key} has no sources; an event nobody reported is not an event")
        if not any(source.role is SourceRole.DISCOVERY for source in self.sources):
            raise MaterialError(
                f"event {self.event_key} has no DISCOVERY source. Something had to find it first, and which "
                "source that was decides whether later reports count as independent confirmation"
            )

    @property
    def first_known_at(self) -> datetime:
        """The earliest moment this system could have used the event.

        Derived, never supplied. The database recomputes it by trigger for the
        same reason: a hand-written knowledge time is a leak waiting to happen.
        """

        return min(source.available_to_model_at for source in self.sources)

    @property
    def independent_source_count(self) -> int:
        """Distinct publishers in a discovering or verifying role.

        Corroboration is excluded deliberately. Twenty outlets reprinting one
        wire story is one source's word repeated twenty times, and counting it as
        twenty confirmations is how a rumour becomes a fact.
        """

        return len(
            {
                source.source_key
                for source in self.sources
                if source.role in (SourceRole.DISCOVERY, SourceRole.VERIFICATION)
            }
        )

    @property
    def is_independently_verified(self) -> bool:
        return self.independent_source_count >= 2


@dataclass(frozen=True)
class EntityRelation:
    """A claim that one event reaches one security, by a named mechanism."""

    relation_type: RelationType
    confidence: LinkConfidence
    extractor: str
    extractor_version: str
    security_id: str | None = None
    issuer_id: str | None = None
    causal_path: str | None = None
    evidence: dict | None = None

    def __post_init__(self) -> None:
        if self.security_id is None and self.issuer_id is None:
            raise MaterialError("an entity relation must name a security or an issuer")
        if self.relation_type in MACRO_RELATIONS and not (self.causal_path or "").strip():
            raise MaterialError(
                f"{self.relation_type} needs a causal_path. A macro exposure with no stated mechanism "
                "cannot be evaluated, defended or falsified, so it is not stored as a claim"
            )


@dataclass(frozen=True)
class EventSecurityFeatures:
    """The seven measurements, for one event against one security.

    Every one of them may be ``None``. Null means "not measurable here", which is
    a different statement from zero, and the difference matters: an event whose
    market reaction cannot be measured is not an event the market ignored.
    """

    event_id: str
    security_id: str
    feature_version: str
    knowledge_cutoff: datetime
    novelty: float | None = None
    surprise: float | None = None
    directness: float | None = None
    magnitude: float | None = None
    persistence: float | None = None
    market_reaction: float | None = None
    priced_in: float | None = None
    methods: dict[str, str] = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    FEATURE_NAMES = (
        "novelty",
        "surprise",
        "directness",
        "magnitude",
        "persistence",
        "market_reaction",
        "priced_in",
    )

    def __post_init__(self) -> None:
        for name in self.FEATURE_NAMES:
            value = getattr(self, name)
            if value is None:
                continue
            if not 0.0 <= value <= 1.0:
                raise MaterialError(f"{name} = {value} is outside [0, 1]; these are normalised measurements")
            if name not in self.methods:
                raise MaterialError(
                    f"{name} has a value but no method. A number nobody can reproduce is not a measurement"
                )

    @property
    def measured(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in self.FEATURE_NAMES if getattr(self, name) is not None}

    @property
    def unmeasured(self) -> tuple[str, ...]:
        return tuple(name for name in self.FEATURE_NAMES if getattr(self, name) is None)
