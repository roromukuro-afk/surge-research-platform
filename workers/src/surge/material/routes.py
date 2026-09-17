"""Material Routes M1-M6: OR-type candidate generation from events.

The counterpart to screening Routes A-H, and built the same way for the same
reason. A security qualifies by firing **any one** route, every route that fired
is recorded, and each one stores the measurements it fired on rather than a
label. An AND of all six would be a filter that finds almost nothing; that is not
what candidate generation is for.

Two rules are structural rather than tuned:

* **WEAK_ASSOCIATION never carries a route.** It appears in no route's accepted
  relation types, and :func:`evaluate_routes` refuses it again at the top. The
  database says so a third time, as a check constraint on the route definitions.
* **Macro routes need a stated mechanism.** M4 and M5 will not fire on an
  exposure claim with no written causal path, because such a claim cannot be
  checked by anyone reading it later.

Thresholds are taken from the route definitions and are **not calibrated**: no
real material data has passed through them yet. They live in ``thresholds`` so
recalibration is a new route_version rather than an edit to this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from surge.material.models import (
    DIRECT_RELATIONS,
    MACRO_RELATIONS,
    EntityRelation,
    EventSecurityFeatures,
    MaterialEvent,
    RelationType,
    strongest,
)

MATERIAL_ROUTE_VERSION = "material-route-1.0.0"

#: Mirrors the seeded ``material.route_definitions`` rows. Kept here as well so
#: the engine can run without a database - and checked against the database by an
#: integration test, so the two cannot drift apart unnoticed.
ROUTE_ACCEPTS: dict[str, frozenset[RelationType]] = {
    "M1": frozenset({RelationType.DIRECT_COMPANY, RelationType.SUBSIDIARY}),
    "M2": frozenset({RelationType.DIRECT_COMPANY, RelationType.SUBSIDIARY, RelationType.PRODUCT}),
    "M3": frozenset(
        {RelationType.CUSTOMER, RelationType.SUPPLIER, RelationType.COMPETITOR, RelationType.PRODUCT}
    ),
    "M4": frozenset({RelationType.POLICY_EXPOSURE, RelationType.RATE_EXPOSURE, RelationType.INDUSTRY}),
    "M5": frozenset(
        {RelationType.COMMODITY_EXPOSURE, RelationType.FX_EXPOSURE, RelationType.GEOPOLITICAL_EXPOSURE}
    ),
    "M6": frozenset(set(RelationType) - {RelationType.WEAK_ASSOCIATION}),
}

THRESHOLDS: dict[str, dict] = {
    "M1": {"min_novelty": 0.5, "document_types": ("TIMELY_DISCLOSURE", "STATUTORY_FILING")},
    "M2": {"min_independent_sources": 2, "min_directness": 0.6},
    "M3": {"min_directness": 0.4, "min_magnitude": 0.4},
    "M4": {"min_magnitude": 0.5, "requires_causal_path": True},
    "M5": {"min_magnitude": 0.5, "requires_causal_path": True},
    "M6": {"min_surprise": 0.7, "max_priced_in": 0.3},
}


@dataclass
class MaterialCandidate:
    """What the material side nominates, and the evidence for each route."""

    security_id: str
    event_ids: list[str] = field(default_factory=list)
    discovery_routes: list[str] = field(default_factory=list)
    route_evidence: dict = field(default_factory=dict)
    strongest_relation: RelationType | None = None
    refusals: dict = field(default_factory=dict)

    @property
    def is_candidate(self) -> bool:
        return bool(self.discovery_routes)

    @property
    def route_count(self) -> int:
        return len(self.discovery_routes)


def _accepted(relations, route_code: str) -> list[EntityRelation]:
    accepted = ROUTE_ACCEPTS[route_code]
    return [relation for relation in relations if relation.relation_type in accepted]


def _has_path(relations) -> bool:
    return any((relation.causal_path or "").strip() for relation in relations)


def _at_least(value: float | None, floor: float) -> bool:
    """A missing measurement does not clear a floor.

    Treating ``None`` as passing would let a feature we could not compute open a
    route, which is the opposite of what a threshold is for.
    """

    return value is not None and value >= floor


def evaluate_routes(
    event: MaterialEvent,
    relations,
    features: EventSecurityFeatures,
    *,
    document_types=(),
) -> tuple[list[str], dict, dict]:
    """Run M1-M6 for one (event, security) pair.

    Returns the routes that fired, the evidence each fired on, and the reasons
    the others did not. The refusals are returned rather than dropped because
    "why did nothing fire today" is a question that gets asked constantly, and
    answering it from stored evidence beats re-running the pipeline to find out.
    """

    relations = [r for r in relations if r.relation_type is not RelationType.WEAK_ASSOCIATION]
    fired: list[str] = []
    evidence: dict = {}
    refusals: dict = {}

    if not relations:
        return [], {}, {"ALL": "no relation other than WEAK_ASSOCIATION, which never carries a route on its own"}

    document_types = tuple(document_types)

    # M1 - the issuer's own regulated disclosure about itself.
    m1 = _accepted(relations, "M1")
    regulated = [d for d in document_types if d in THRESHOLDS["M1"]["document_types"]]
    if m1 and regulated and _at_least(features.novelty, THRESHOLDS["M1"]["min_novelty"]):
        fired.append("M1")
        evidence["M1"] = {
            "relation": strongest(r.relation_type for r in m1),
            "document_types": regulated,
            "novelty": features.novelty,
        }
    else:
        refusals["M1"] = (
            f"relations={len(m1)}, regulated_documents={len(regulated)}, novelty={features.novelty}"
        )

    # M2 - company news confirmed by a second, independent publisher.
    m2 = _accepted(relations, "M2")
    independent = event.independent_source_count
    if (
        m2
        and independent >= THRESHOLDS["M2"]["min_independent_sources"]
        and _at_least(features.directness, THRESHOLDS["M2"]["min_directness"])
    ):
        fired.append("M2")
        evidence["M2"] = {
            "relation": strongest(r.relation_type for r in m2),
            "independent_sources": independent,
            "directness": features.directness,
        }
    else:
        refusals["M2"] = f"relations={len(m2)}, independent_sources={independent}, directness={features.directness}"

    # M3 - reaches the issuer through a named commercial relationship.
    m3 = _accepted(relations, "M3")
    if (
        m3
        and _at_least(features.directness, THRESHOLDS["M3"]["min_directness"])
        and _at_least(features.magnitude, THRESHOLDS["M3"]["min_magnitude"])
    ):
        fired.append("M3")
        evidence["M3"] = {
            "relation": strongest(r.relation_type for r in m3),
            "directness": features.directness,
            "magnitude": features.magnitude,
        }
    else:
        refusals["M3"] = f"relations={len(m3)}, directness={features.directness}, magnitude={features.magnitude}"

    # M4 and M5 - macro exposure, and only with a written mechanism.
    for code in ("M4", "M5"):
        accepted = _accepted(relations, code)
        has_path = _has_path(accepted)
        if accepted and has_path and _at_least(features.magnitude, THRESHOLDS[code]["min_magnitude"]):
            fired.append(code)
            evidence[code] = {
                "relation": strongest(r.relation_type for r in accepted),
                "causal_path": next((r.causal_path for r in accepted if (r.causal_path or "").strip()), None),
                "magnitude": features.magnitude,
            }
        else:
            refusals[code] = (
                f"relations={len(accepted)}, causal_path={has_path}, magnitude={features.magnitude}"
            )

    # M6 - a surprise the price has not taken. priced_in must be MEASURED: an
    # unmeasurable priced-in is not evidence that nothing is priced in.
    m6 = _accepted(relations, "M6")
    priced_in = features.priced_in
    if (
        m6
        and _at_least(features.surprise, THRESHOLDS["M6"]["min_surprise"])
        and priced_in is not None
        and priced_in <= THRESHOLDS["M6"]["max_priced_in"]
    ):
        fired.append("M6")
        evidence["M6"] = {
            "relation": strongest(r.relation_type for r in m6),
            "surprise": features.surprise,
            "priced_in": priced_in,
        }
    else:
        refusals["M6"] = f"relations={len(m6)}, surprise={features.surprise}, priced_in={priced_in}"

    return fired, evidence, refusals


def build_candidate(security_id: str, evaluations) -> MaterialCandidate:
    """Fold every event touching one security into a single candidate row.

    Routes are unioned across events on purpose. A security reached by a policy
    change through one event and a supplier failure through another has fired two
    routes, and the pair is more interesting than either alone - so both stay.
    """

    candidate = MaterialCandidate(security_id=security_id)
    relation_types: list[RelationType] = []

    for event, relations, features, document_types in evaluations:
        fired, evidence, refusals = evaluate_routes(event, relations, features, document_types=document_types)
        event_id = event.event_id or event.event_key
        if fired:
            if event_id not in candidate.event_ids:
                candidate.event_ids.append(event_id)
            for code in fired:
                if code not in candidate.discovery_routes:
                    candidate.discovery_routes.append(code)
            candidate.route_evidence[event_id] = evidence
            relation_types.extend(
                r.relation_type for r in relations if r.relation_type is not RelationType.WEAK_ASSOCIATION
            )
        else:
            candidate.refusals[event_id] = refusals

    candidate.discovery_routes.sort()
    candidate.strongest_relation = strongest(relation_types)
    return candidate


def route_summary(candidates) -> dict[str, int]:
    counts = {code: 0 for code in sorted(ROUTE_ACCEPTS)}
    for candidate in candidates:
        for code in candidate.discovery_routes:
            counts[code] += 1
    counts["candidates"] = sum(1 for candidate in candidates if candidate.is_candidate)
    return counts


def macro_routes_require_a_mechanism() -> bool:
    """Self-check used by the tests: every macro route demands a causal path."""

    return all(
        THRESHOLDS[code].get("requires_causal_path") is True
        for code in ("M4", "M5")
    ) and all(ROUTE_ACCEPTS[code] <= MACRO_RELATIONS for code in ("M4", "M5"))


def direct_routes_stay_direct() -> bool:
    return ROUTE_ACCEPTS["M1"] <= DIRECT_RELATIONS and ROUTE_ACCEPTS["M2"] <= DIRECT_RELATIONS
