"""Turning a TDnet index row into something the material routes can read.

The interesting constraint is how little a title honestly supports. Of the seven
Phase 5 features, exactly two can be measured from a headline:

``directness``
    A regulated disclosure filed by the issuer about itself, identified by its
    own security code. There is no inference chain to discount, so this is 1.0
    and the method says why.
``novelty``
    Whether this is the first time the issuer has said this, approximated by the
    disclosure type and the correction/progress flags. Crude, and labelled crude.

The other five - surprise, magnitude, persistence, market reaction, priced-in -
are **None**. None means "not measurable here", and the route engine refuses a
missing feature rather than treating it as a pass. The practical consequence is
that a title-only discovery can fire M1, which asks only for a regulated
disclosure with novelty, and cannot fire M2, M3 or M6, which ask for things a
headline does not contain. That is the design working, not a gap in it: the
missing numbers arrive when a verification source does.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from surge.jobs.tdnet_discovery import DiscoveredItem
from surge.material.models import (
    EntityRelation,
    EventSecurityFeatures,
    EventSource,
    LinkConfidence,
    MaterialEvent,
    RelationType,
    SourceRole,
)
from surge.material.tdnet_classify import CLASSIFIER_VERSION, Classification, DisclosureType
from surge.news.models import DocumentType, TimePrecision
from surge.news.sources.yanoshin_tdnet import SOURCE_KEY

MERGE_VERSION = "event-merge-1.0.0"
FEATURE_VERSION = f"tdnet-title-features-1.0.0+{CLASSIFIER_VERSION}"

#: Disclosure types the issuer files about itself, where the relation to the
#: security is the filing itself rather than an inference from it.
_DIRECT_TYPES = frozenset(set(DisclosureType) - {DisclosureType.OTHER})


def _novelty(classification: Classification) -> tuple[float, str]:
    """How new this is, as far as a title can say.

    Deliberately three coarse bands rather than a continuous score. A finer
    number would imply the title supports a precision it does not.
    """

    if classification.is_routine:
        return 0.10, (
            "routine disclosure type published on a fixed cadence; the title carries no novelty "
            "(tdnet-title band: routine)"
        )
    if classification.is_correction or classification.is_progress_update:
        return 0.30, (
            "an update to something already disclosed, so the event is known and only its detail is "
            "new (tdnet-title band: update)"
        )
    return 0.60, (
        "a first disclosure of its type from this issuer, as far as the title shows; not a measurement "
        "of how surprising the contents are (tdnet-title band: first)"
    )


def event_key_for(item: DiscoveredItem) -> str:
    """One event per index row, keyed on the service's own item id.

    Not a content hash and not a title fingerprint: the id is the service's
    identity for the disclosure, and using anything derived from text would
    merge two different disclosures that happened to be titled alike - the false
    merge the project forbids.
    """

    return f"tdnet:{item.item.yanoshin_id}"


def to_event(item: DiscoveredItem, *, document_id: str | None = None) -> MaterialEvent:
    times = item.document.times
    return MaterialEvent(
        event_key=event_key_for(item),
        event_type=item.classification.disclosure_type.value,
        scope="JP",
        merge_version=MERGE_VERSION,
        sources=(
            EventSource(
                document_id=document_id or str(item.item.yanoshin_id),
                source_key=SOURCE_KEY,
                # Discovery, and only discovery. An unofficial index of a source
                # we cannot read cannot also confirm what the document said.
                role=SourceRole.DISCOVERY,
                available_to_model_at=times.available_to_model_at,
                match_evidence=item.classification.as_evidence(),
            ),
        ),
        headline=item.item.title,
        occurred_at=item.item.pubdate,
        occurred_at_precision=TimePrecision.EXACT,
        event_id=event_key_for(item),
    )


def to_relation(item: DiscoveredItem) -> EntityRelation | None:
    """The link from the disclosure to the security, or None if unmapped.

    Confidence is REGISTRY_ANCHORED rather than STRONG: the security code came
    from the exchange, which is a registry, but it reached us through a third
    party and was normalised by a rule of ours. That is one step removed from
    reading the identifier off the registry directly, and the vocabulary has a
    word for it.
    """

    if item.security_id is None:
        return None
    return EntityRelation(
        relation_type=RelationType.DIRECT_COMPANY,
        confidence=LinkConfidence.REGISTRY_ANCHORED,
        extractor="tdnet-discovery",
        extractor_version=FEATURE_VERSION,
        security_id=item.security_id,
        evidence={
            "raw_company_code": item.item.code.raw,
            "normalised_company_code": item.item.code.normalised,
            "code_normalisation": item.item.code.kind.value,
            "company_name": item.item.company_name,
            "disclosure_type": item.classification.disclosure_type.value,
        },
    )


def to_features(item: DiscoveredItem, *, knowledge_cutoff: datetime) -> EventSecurityFeatures | None:
    if item.security_id is None:
        return None

    novelty, novelty_method = _novelty(item.classification)
    direct = item.classification.disclosure_type in _DIRECT_TYPES

    return EventSecurityFeatures(
        event_id=event_key_for(item),
        security_id=item.security_id,
        feature_version=FEATURE_VERSION,
        knowledge_cutoff=knowledge_cutoff,
        novelty=novelty,
        directness=1.0 if direct else 0.5,
        # The remaining five need the document, the price, or both. None means
        # not measurable, which the route engine treats as failing a threshold
        # rather than passing one.
        surprise=None,
        magnitude=None,
        persistence=None,
        market_reaction=None,
        priced_in=None,
        methods={
            "novelty": novelty_method,
            "directness": (
                "the issuer's own regulated disclosure, identified by its own security code; no "
                "inference chain stands between the filing and the security"
                if direct
                else "the disclosure type could not be recognised from the title, so directness is "
                "held at the midpoint rather than assumed"
            ),
        },
        evidence={
            "classifier": item.classification.as_evidence(),
            "title": item.item.title,
            "pubdate": item.item.pubdate.isoformat(),
            "unmeasurable_from_a_title": [
                "surprise",
                "magnitude",
                "persistence",
                "market_reaction",
                "priced_in",
            ],
        },
    )


def to_material_evaluations(
    items: Sequence[DiscoveredItem],
    *,
    knowledge_cutoff: datetime,
) -> dict[str, list[tuple]]:
    """Group discoveries into the shape ``build_candidate`` expects.

    Keyed by security id, so one issuer disclosing three things in a day arrives
    as one candidate with three contributing events rather than three candidates.
    Unmapped rows are dropped here and counted upstream: a disclosure we cannot
    attach to a security cannot become a candidate, but it is still stored, and
    ``unmapped_company_codes`` is what makes that visible.
    """

    grouped: dict[str, list[tuple]] = {}
    for item in items:
        relation = to_relation(item)
        features = to_features(item, knowledge_cutoff=knowledge_cutoff)
        if relation is None or features is None:
            continue
        document_types = (
            (DocumentType.TIMELY_DISCLOSURE.value,)
            if item.document.document_type is DocumentType.TIMELY_DISCLOSURE
            else ()
        )
        grouped.setdefault(item.security_id, []).append(
            (to_event(item), [relation], features, document_types)
        )
    return grouped


def events_by_id(items: Sequence[DiscoveredItem]) -> dict[str, MaterialEvent]:
    return {event_key_for(item): to_event(item) for item in items if item.security_id is not None}
