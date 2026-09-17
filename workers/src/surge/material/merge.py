"""Turning documents into events, with a bias the project already settled.

The security identity model chose false split over false merge, because a split
can be joined later and a merge cannot be undone. The same asymmetry applies to
events, and more sharply: two separate disclosures merged into one event lose a
piece of news outright, whereas one event recorded twice is visible, countable
and fixable.

So merging happens only on evidence:

1. **A shared precise occurrence time.** Two outlets reporting the same event at
   the same published minute, about the same entity, are reporting one event.
2. **An explicit cross-reference.** One document citing another's URL.

Everything else stays split, and the pairs that *looked* similar are reported as
merge candidates rather than quietly joined. A split nobody can see is as bad as
a bad merge; a split with a list attached is a work queue.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from surge.material.models import EventSource, MaterialError, MaterialEvent, SourceRole
from surge.news.models import TimePrecision

MERGE_VERSION = "event-merge-1.0.0"

#: How far apart two precise timestamps may be and still be one event. Outlets
#: republish a release within a minute of each other; an hour apart is two
#: events, or one event and a follow-up, and we do not get to decide which.
PRECISE_MATCH_WINDOW = timedelta(minutes=2)

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise_title(title: str | None) -> str:
    """Fold a headline for comparison only - never for identity.

    Used to propose merge candidates for review. It is deliberately not part of
    the event key: the project forbids normalised text as an identity, for
    securities and for the same reason here.
    """

    if not title:
        return ""
    folded = unicodedata.normalize("NFKC", title).casefold()
    folded = _PUNCTUATION.sub(" ", folded)
    return _WHITESPACE.sub(" ", folded).strip()


def title_tokens(title: str | None) -> frozenset[str]:
    return frozenset(normalise_title(title).split())


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True)
class DocumentFacts:
    """What the extractor established about one document, ready to be merged."""

    document_id: str
    source_key: str
    scope: str
    event_type: str
    entity_key: str | None
    available_to_model_at: object
    published_at: object = None
    published_precision: TimePrecision = TimePrecision.UNKNOWN
    title: str | None = None
    references: tuple[str, ...] = ()
    role: SourceRole = SourceRole.DISCOVERY


def event_key(facts: DocumentFacts) -> str:
    """The deduplication key.

    Two documents get the same key only when they share an entity and a precise
    publication minute. Without a precise time the key falls back to the document
    itself, which splits - the safe direction.
    """

    parts = [MERGE_VERSION, facts.scope, facts.event_type, facts.entity_key or "UNRESOLVED"]
    if facts.entity_key and facts.published_precision is TimePrecision.EXACT and facts.published_at is not None:
        bucket = facts.published_at.replace(second=0, microsecond=0).isoformat()
        parts.append(f"at:{bucket}")
    else:
        parts.append(f"doc:{facts.document_id}")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()[:32]


def _same_minute(left: DocumentFacts, right: DocumentFacts) -> bool:
    if left.published_at is None or right.published_at is None:
        return False
    if TimePrecision.EXACT not in (left.published_precision, right.published_precision):
        return False
    if left.published_precision is not right.published_precision:
        return False
    return abs(left.published_at - right.published_at) <= PRECISE_MATCH_WINDOW


def _cross_references(left: DocumentFacts, right: DocumentFacts) -> bool:
    return left.document_id in right.references or right.document_id in left.references


def merge_documents(facts) -> tuple[list[MaterialEvent], list[dict]]:
    """Group documents into events, and report the near-misses.

    Returns the events and a list of merge candidates: pairs that were left
    separate but share an entity, an event type and a similar headline. Those are
    for a human to look at, not for the pipeline to act on.
    """

    facts = list(facts)
    grouped: dict[str, list[DocumentFacts]] = defaultdict(list)
    for fact in facts:
        grouped[event_key(fact)].append(fact)

    # A cross-reference joins two groups that the key left apart.
    keys = list(grouped)
    for index, left_key in enumerate(keys):
        for right_key in keys[index + 1 :]:
            if left_key not in grouped or right_key not in grouped:
                continue
            if any(
                _cross_references(left, right)
                for left in grouped[left_key]
                for right in grouped[right_key]
            ):
                grouped[left_key].extend(grouped.pop(right_key))

    events: list[MaterialEvent] = []
    for key, members in grouped.items():
        ordered = sorted(members, key=lambda f: f.available_to_model_at)
        sources = tuple(
            EventSource(
                document_id=fact.document_id,
                source_key=fact.source_key,
                # The first document to reach us discovered it; a later document
                # from a DIFFERENT publisher verifies it; a later one from the
                # same publisher only corroborates. Counting a publisher's own
                # follow-up as confirmation is how one source becomes two.
                role=(
                    SourceRole.DISCOVERY
                    if position == 0
                    else (
                        SourceRole.VERIFICATION
                        if fact.source_key not in {earlier.source_key for earlier in ordered[:position]}
                        else SourceRole.CORROBORATION
                    )
                ),
                available_to_model_at=fact.available_to_model_at,
            )
            for position, fact in enumerate(ordered)
        )
        first = ordered[0]
        events.append(
            MaterialEvent(
                event_key=key,
                event_type=first.event_type,
                scope=first.scope,
                merge_version=MERGE_VERSION,
                sources=sources,
                headline=first.title,
                occurred_at=first.published_at,
                occurred_at_precision=first.published_precision,
            )
        )

    return events, merge_candidates(grouped)


def merge_candidates(grouped, *, similarity_floor: float = 0.6) -> list[dict]:
    """Pairs we did not merge that a person might want to look at."""

    representatives = {key: members[0] for key, members in grouped.items() if members}
    candidates: list[dict] = []
    keys = sorted(representatives)
    for index, left_key in enumerate(keys):
        left = representatives[left_key]
        for right_key in keys[index + 1 :]:
            right = representatives[right_key]
            if left.entity_key is None or left.entity_key != right.entity_key:
                continue
            if left.event_type != right.event_type:
                continue
            similarity = jaccard(title_tokens(left.title), title_tokens(right.title))
            if similarity < similarity_floor:
                continue
            candidates.append(
                {
                    "left_event_key": left_key,
                    "right_event_key": right_key,
                    "entity_key": left.entity_key,
                    "event_type": left.event_type,
                    "title_similarity": round(similarity, 3),
                    "same_precise_minute": _same_minute(left, right),
                    "reason": (
                        "same entity and event type with similar headlines, but no shared precise "
                        "publication minute and no cross-reference. Left split deliberately; "
                        "review before joining."
                    ),
                }
            )
    return candidates


def assert_no_silent_merge(events) -> None:
    """Guard for callers that build events by hand rather than through merging."""

    for event in events:
        if event.merge_version != MERGE_VERSION:
            raise MaterialError(
                f"event {event.event_key} was built under {event.merge_version}, not {MERGE_VERSION}. "
                "Events from different merge rules must not be mixed in one run"
            )
