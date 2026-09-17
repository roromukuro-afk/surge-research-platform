"""Phase 4: news and disclosure collection.

The public surface is deliberately small: describe a source, say what its licence
permits, and run :func:`~surge.news.collector.collect`. Everything that decides
what a model may know lives in :mod:`surge.news.models`.
"""

from surge.news.collector import (
    COLLECTOR_VERSION,
    CollectionResult,
    KnownDocument,
    NewsSource,
    SourcePolicy,
    SourceSpec,
    collect,
    content_fingerprint,
)
from surge.news.feeds import FeedError, parse_datetime, parse_feed
from surge.news.models import (
    AccessMechanism,
    AuthRequirement,
    AvailabilityBasis,
    BodyStorage,
    CollectedDocument,
    DocumentType,
    FeedItem,
    KnowledgeTimes,
    SourceKind,
    TimeError,
    TimePrecision,
)

__all__ = [
    "COLLECTOR_VERSION",
    "AccessMechanism",
    "AuthRequirement",
    "AvailabilityBasis",
    "BodyStorage",
    "CollectedDocument",
    "CollectionResult",
    "DocumentType",
    "FeedError",
    "FeedItem",
    "KnowledgeTimes",
    "KnownDocument",
    "NewsSource",
    "SourceKind",
    "SourcePolicy",
    "SourceSpec",
    "TimeError",
    "TimePrecision",
    "collect",
    "content_fingerprint",
    "parse_datetime",
    "parse_feed",
]
