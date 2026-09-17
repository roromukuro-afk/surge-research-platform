"""Routes A-H: OR-type candidate generation over the feature record."""

from surge.routes.engine import (
    ROUTE_DEFINITIONS,
    ROUTE_VERSION,
    CandidateResult,
    RouteDefinition,
    RouteHit,
    evaluate_routes,
    route_summary,
)

__all__ = [
    "ROUTE_DEFINITIONS",
    "ROUTE_VERSION",
    "CandidateResult",
    "RouteDefinition",
    "RouteHit",
    "evaluate_routes",
    "route_summary",
]
