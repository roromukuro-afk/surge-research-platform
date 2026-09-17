"""The noise filter, and why it is not a keyword list.

Keyword exclusion is forbidden by the project rules, and the reason is worth
stating plainly: a keyword filter drops events for a property of their wording
rather than of their content, so it fails in both directions at once. It removes
a real disclosure that happens to mention a blocked word, and it keeps a press
release about an office move because nothing in it matched.

So a verdict here rests on structural signals - did we resolve an entity, is
there a stated mechanism, is the source a regulated disclosure, is a magnitude
quantified - and every verdict carries the signals it rested on, so a later
reader can disagree with it on the evidence rather than on faith.

Two states earn their keep:

``UNCERTAIN``
    We could not establish relevance, which is not the same as establishing
    irrelevance. As with ``UNRESOLVED`` in the universe definition, uncertain is
    not a synonym for excluded: it is kept, counted and reviewable.
``NOT_RELEVANT``
    Reserved for the one case we can actually assert - there is no security this
    event can be attached to at all. Even then the event stays in the database;
    only candidate generation passes it by.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from surge.material.models import (
    DIRECT_RELATIONS,
    MACRO_RELATIONS,
    MarketRelevance,
    MaterialEvent,
    RelationType,
    strongest,
)

RELEVANCE_RULESET_VERSION = "relevance-1.0.0"


@dataclass(frozen=True)
class RelevanceSignals:
    """What the verdict is allowed to rest on. All structural, none lexical."""

    resolved_entities: int
    strongest_relation: RelationType | None
    relations_with_causal_path: int
    from_regulated_disclosure: bool
    has_quantified_magnitude: bool
    independent_source_count: int
    document_count: int

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["strongest_relation"] = self.strongest_relation.value if self.strongest_relation else None
        return payload


@dataclass(frozen=True)
class RelevanceDecision:
    event_key: str
    relevance: MarketRelevance
    reason_code: str
    reason_detail: str
    signals: RelevanceSignals
    ruleset_version: str = RELEVANCE_RULESET_VERSION

    @property
    def blocks_candidacy(self) -> bool:
        """Whether this verdict stops the event generating a candidate.

        ``UNCERTAIN`` blocks candidacy but not storage or review. The event is
        still there, still counted, and still visible to a human deciding whether
        the extractor missed something.
        """

        return self.relevance is not MarketRelevance.RELEVANT


#: Document types that are market-relevant by construction: an issuer filing a
#: regulated disclosure about itself is not incidental coverage.
REGULATED_DOCUMENT_TYPES = frozenset({"TIMELY_DISCLOSURE", "STATUTORY_FILING"})


def build_signals(
    event: MaterialEvent,
    relations,
    *,
    document_types=(),
    quantified_magnitude: bool = False,
) -> RelevanceSignals:
    relations = list(relations)
    return RelevanceSignals(
        resolved_entities=len({(r.security_id, r.issuer_id) for r in relations}),
        strongest_relation=strongest(r.relation_type for r in relations),
        relations_with_causal_path=sum(1 for r in relations if (r.causal_path or "").strip()),
        from_regulated_disclosure=any(document_type in REGULATED_DOCUMENT_TYPES for document_type in document_types),
        has_quantified_magnitude=quantified_magnitude,
        independent_source_count=event.independent_source_count,
        document_count=len(event.sources),
    )


def decide(event: MaterialEvent, signals: RelevanceSignals) -> RelevanceDecision:
    """Apply ``relevance-1.0.0``.

    The order of the rules is the order of confidence: the things we can assert
    come before the things we can only suspect.
    """

    def verdict(relevance: MarketRelevance, code: str, detail: str) -> RelevanceDecision:
        return RelevanceDecision(
            event_key=event.event_key,
            relevance=relevance,
            reason_code=code,
            reason_detail=detail,
            signals=signals,
        )

    if signals.resolved_entities == 0:
        return verdict(
            MarketRelevance.NOT_RELEVANT,
            "NO_RESOLVABLE_ENTITY",
            "no security or issuer could be attached to this event, so there is nothing to act on. "
            "The event is retained; only candidate generation passes it by.",
        )

    relation = signals.strongest_relation

    if signals.from_regulated_disclosure and relation in DIRECT_RELATIONS:
        return verdict(
            MarketRelevance.RELEVANT,
            "REGULATED_DISCLOSURE_ABOUT_KNOWN_ISSUER",
            "a regulated disclosure filed about a resolved issuer. No inference chain stands between "
            "the document and the security.",
        )

    if relation in DIRECT_RELATIONS:
        return verdict(
            MarketRelevance.RELEVANT,
            "DIRECT_RELATION",
            f"the event is about the company itself or its immediate commercial surface ({relation}).",
        )

    if relation in MACRO_RELATIONS:
        if signals.relations_with_causal_path > 0:
            return verdict(
                MarketRelevance.RELEVANT,
                "MACRO_WITH_STATED_MECHANISM",
                f"{relation} with a written transmission path, which makes the claim checkable.",
            )
        return verdict(
            MarketRelevance.UNCERTAIN,
            "MACRO_WITHOUT_MECHANISM",
            f"{relation} with no stated mechanism. The mechanism may well exist and the extractor may "
            "simply have missed it, so this is held for review rather than discarded.",
        )

    if relation is RelationType.WEAK_ASSOCIATION:
        return verdict(
            MarketRelevance.UNCERTAIN,
            "WEAK_ASSOCIATION_ONLY",
            "the only link found was a weak association, which never carries an event on its own. "
            "Held for review: a weak link is often a strong one the extractor could not name.",
        )

    return verdict(
        MarketRelevance.UNCERTAIN,
        "UNCLASSIFIED",
        f"relation {relation} did not match any rule in {RELEVANCE_RULESET_VERSION}. "
        "Recorded as uncertain so the gap in the ruleset is visible rather than silent.",
    )


def summarise(decisions) -> dict[str, int]:
    counts = {relevance.value: 0 for relevance in MarketRelevance}
    reasons: dict[str, int] = {}
    for decision in decisions:
        counts[decision.relevance.value] += 1
        reasons[decision.reason_code] = reasons.get(decision.reason_code, 0) + 1
    counts.update({f"reason:{code}": total for code, total in sorted(reasons.items())})
    return counts
