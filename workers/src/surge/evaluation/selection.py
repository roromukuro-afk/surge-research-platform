"""Phase B's daily sample (``phase-b-selection-1.0.0``; D-272 as the user decided it on 2026-09-19).

A Phase B cohort freezes this file (``surge.evaluation.phase_b``): changing it
means a new cohort, never a changed one.

Every TSE business day from ``PHASE_B_START`` the screener's verdict on every
security at no more than 3,000 yen (its S0 close as traded) is kept - passing
and not, with each route's membership - and only then is the day's sample
drawn from it:

* **Primary**, at most 60: a uniform sample of the day's passing securities,
  drawn deterministically. Each candidate's key is the SHA-256 of the
  evaluation version, the experiment seed, S0 and its code, and the lowest
  keys are taken - all of them when 60 or fewer pass. Nothing else enters the
  key: not a route, a price, an outcome or an answer, so a candidate that
  passed Route D alone is exactly as likely as any other. Every candidate is
  kept with its key, its rank, whether it was taken and the probability that
  it would be.
* **Control**, at most 20 (one for every three Primary): from the same day's
  eligible securities that passed no route, matched to a hashed choice of the
  selected Primary on price band and liquidity band (the day's quartile of
  20-day turnover), the same 33-industry sector where one is available, and
  the lowest key among equals. The sector is a preference inside a price and
  liquidity match, so it never costs a sample.
* **Anonymized and drift**: 10% and 5% of the day's main samples (8 and 4 of
  80), disjoint, by hash.

That is at most 92 requests a day. Route D is not changed, excluded or
down-weighted: each Primary sample is labelled with its pre-registered
subgroup - D only, D and another route, no D - and the subgroups are reported
apart.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date

from surge.evaluation.population import Sample, Screen, liquidity_bands, price_band, sample_id
from surge.evaluation.universe import ListedIssue
from surge.routes.engine import ROUTE_DEFINITIONS

SELECTION_VERSION = "phase-b-selection-1.0.0"
#: The first S0 of Phase B. 2026-09-21..23 were not TSE sessions; nothing before is used.
PHASE_B_START = date(2026, 9, 24)
PRIMARY_PER_DAY = 60
CONTROL_PER_DAY = 20
MAIN_PER_DAY = PRIMARY_PER_DAY + CONTROL_PER_DAY
ANONYMIZED_PER_DAY = MAIN_PER_DAY // 10
DRIFT_PER_DAY = MAIN_PER_DAY // 20
MAX_REQUESTS_PER_DAY = MAIN_PER_DAY + ANONYMIZED_PER_DAY + DRIFT_PER_DAY
ROUTES = tuple(sorted(ROUTE_DEFINITIONS))
ROUTE_D = "D"
SUBGROUPS = ("D_ONLY", "D_PLUS_OTHER", "NON_D")
#: How close a Control is to its Primary, best first.
MATCH_TIERS = ("PRICE+LIQUIDITY+SECTOR", "PRICE+LIQUIDITY", "PRICE", "LIQUIDITY", "NONE")
SUBGROUP_DEFINITION = {
    "D_ONLY": "Route D is the only route that fired",
    "D_PLUS_OTHER": "Route D fired together with at least one other route",
    "NON_D": "at least one route fired, Route D did not",
}


class SelectionError(ValueError):
    pass


def route_d_subgroup(routes) -> str | None:
    """The pre-registered Route D subgroup of a passing security; None when no route fired."""

    fired = set(routes or ())
    if not fired:
        return None
    if fired == {ROUTE_D}:
        return "D_ONLY"
    return "D_PLUS_OTHER" if ROUTE_D in fired else "NON_D"


def selection_key(evaluation_version: str, experiment_seed: str, s0: date, purpose: str, *parts: str) -> str:
    """The deterministic, uniform ordering key: nothing but these strings enters it."""

    text = "|".join([evaluation_version, str(experiment_seed), s0.isoformat(), purpose, *parts])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def control_count(primary_selected: int) -> int:
    """One Control for every three Primary, rounded up, at most 20."""

    return min(CONTROL_PER_DAY, -(-primary_selected * CONTROL_PER_DAY // PRIMARY_PER_DAY))


def anonymized_count(main: int) -> int:
    return min(ANONYMIZED_PER_DAY, main // 10)


def drift_count(main: int) -> int:
    return min(DRIFT_PER_DAY, main // 20)


def check_s0(s0: date) -> None:
    if s0 < PHASE_B_START:
        raise SelectionError(f"S0 {s0.isoformat()} is before Phase B's prospective start "
                             f"{PHASE_B_START.isoformat()}; earlier dates are not used")
    if s0.weekday() >= 5:
        raise SelectionError(f"S0 {s0.isoformat()} is a weekend day")


@dataclass(frozen=True)
class DaySelection:
    samples: list[Sample]
    candidates: list[dict]
    summary: dict


def select_day(screens: list[Screen], issues: dict[str, ListedIssue], *, s0: date, evaluation_version: str,
               experiment_seed: str) -> DaySelection:
    """The day's Primary, Control, anonymized and drift samples, and every candidate as it was considered."""

    check_s0(s0)
    eligible = sorted((s for s in screens if s.eligible and s.code in issues), key=lambda s: s.code)
    if any(s.s0 != s0 for s in eligible):
        raise SelectionError("every screen of the day must be of its S0")
    if len({s.code for s in eligible}) != len(eligible):
        raise SelectionError("a security was screened twice on one day")

    def key(purpose: str, *parts: str) -> str:
        return selection_key(evaluation_version, experiment_seed, s0, purpose, *parts)

    bands = liquidity_bands(eligible)
    passing = [s for s in eligible if s.passed]
    others = [s for s in eligible if not s.passed]

    # Primary: the lowest keys, a uniform sample of the passing securities.
    primary_keys = {s.code: key("PRIMARY", s.code) for s in passing}
    ranked = sorted(passing, key=lambda s: primary_keys[s.code])
    chosen = ranked[:PRIMARY_PER_DAY]
    probability = min(1.0, PRIMARY_PER_DAY / len(passing)) if passing else None

    def make(screen: Screen, cohort: str, **extra) -> Sample:
        issue = issues[screen.code]
        return Sample(
            sample_id=sample_id(screen.code, s0, cohort), cohort=cohort, code=screen.code, name=issue.name,
            segment=issue.segment, sector33=issue.sector33, size_category=issue.size_category, s0=s0,
            s0_close_as_traded=screen.close_as_traded, price_band=price_band(screen.close_as_traded),
            liquidity_band=bands.get(screen.code, "unknown"), routes=list(screen.routes),
            route_evidence=screen.route_evidence, **extra,
        )

    primary = [make(s, "PRIMARY", route_d_subgroup=route_d_subgroup(s.routes), selection_probability=probability)
               for s in chosen]

    # Control: matched to a hashed choice of the selected Primary, one each.
    anchors = sorted(zip(chosen, primary, strict=True), key=lambda pair: key("ANCHOR", pair[0].code))
    anchors = anchors[:control_count(len(chosen))]
    control_keys = {s.code: key("CONTROL", s.code) for s in others}
    taken: dict[str, tuple[Sample, str]] = {}
    control = []
    for anchor_screen, anchor in anchors:
        want_price, want_liquidity = anchor.price_band, bands.get(anchor_screen.code)
        want_sector = issues[anchor_screen.code].sector33

        def tier(s: Screen, price=want_price, liquidity=want_liquidity, sector=want_sector) -> int:
            same_price = price_band(s.close_as_traded) == price
            same_liquidity = liquidity is not None and bands.get(s.code) == liquidity
            if same_price and same_liquidity:
                return 0 if issues[s.code].sector33 == sector else 1
            return 2 if same_price else 3 if same_liquidity else 4

        pool = [s for s in others if s.code not in taken]
        if not pool:
            break
        best = min(pool, key=lambda s: (tier(s), control_keys[s.code]))
        match = MATCH_TIERS[tier(best)]
        sample = make(best, "CONTROL", matched_to=anchor.sample_id, match_tier=match)
        taken[best.code] = (sample, match)
        control.append(sample)

    # The sensitivity requests: disjoint, by hash, from the day's main samples.
    main = primary + control
    order = sorted(main, key=lambda s: key("SENSITIVITY", s.sample_id))
    n_anonymized, n_drift = anonymized_count(len(main)), drift_count(len(main))
    anonymized = {s.sample_id for s in order[:n_anonymized]}
    drift = {s.sample_id for s in order[n_anonymized:n_anonymized + n_drift]}
    samples = [replace(s, anonymized_pair=s.sample_id in anonymized, drift_repeat=s.sample_id in drift) for s in main]
    requests = len(samples) + len(anonymized) + len(drift)
    if len(primary) > PRIMARY_PER_DAY or len(control) > CONTROL_PER_DAY or requests > MAX_REQUESTS_PER_DAY:
        raise SelectionError(f"{len(primary)} / {len(control)} / {requests} is past the day's limits")

    rank = {s.code: i for i, s in enumerate(ranked, start=1)}
    selected = {s.code: s for s in primary}
    candidates = []
    for s in eligible:
        issue = issues[s.code]
        in_primary_pool = s.passed
        sample = selected.get(s.code) if in_primary_pool else taken.get(s.code, (None, None))[0]
        candidates.append({
            "s0": s0.isoformat(),
            "code": s.code,
            "pool": "PRIMARY_POOL" if in_primary_pool else "CONTROL_POOL",
            "passed": s.passed,
            "routes": list(s.routes),
            **{f"route_{r}": r in s.routes for r in ROUTES},
            "route_d_subgroup": route_d_subgroup(s.routes),
            "d_only": route_d_subgroup(s.routes) == "D_ONLY",
            "close_as_traded": str(s.close_as_traded),
            "price_band": price_band(s.close_as_traded),
            "liquidity_band": bands.get(s.code, "unknown"),
            "turnover_avg_20d": s.turnover_avg_20d,
            "segment": issue.segment,
            "sector33": issue.sector33,
            "size_category": issue.size_category,
            "selection_key": primary_keys[s.code] if in_primary_pool else control_keys[s.code],
            "selection_rank": rank.get(s.code),
            "selected": sample is not None,
            "selection_probability": probability if in_primary_pool else None,
            "sample_id": None if sample is None else sample.sample_id,
            "matched_to": None if sample is None else sample.matched_to,
            "match_tier": None if in_primary_pool or sample is None else taken[s.code][1],
            "teacher_admissible": False,
        })

    summary = {
        "s0": s0.isoformat(),
        "eligible": len(eligible),
        "passing": len(passing),
        "not_passing": len(others),
        "passing_share": len(passing) / len(eligible) if eligible else None,
        "route_membership_among_passing": {r: sum(r in s.routes for s in passing) for r in ROUTES},
        "route_d_subgroups_among_passing": {g: sum(route_d_subgroup(s.routes) == g for s in passing)
                                            for g in SUBGROUPS},
        "route_d_subgroups_selected": {g: sum(s.route_d_subgroup == g for s in primary) for g in SUBGROUPS},
        "primary_selection_probability": probability,
        "selected": {"primary": len(primary), "control": len(control), "anonymized": len(anonymized),
                     "drift": len(drift)},
        "requests": requests,
        "control_match_tiers": dict(Counter(s.match_tier for s in control)),
    }
    return DaySelection(samples=samples, candidates=candidates, summary=summary)


def selection_signature(samples: list[Sample]) -> list[tuple]:
    """What must come out the same when a day's selection is drawn again from its stored screening."""

    return sorted((s.sample_id, s.cohort, s.code, s.matched_to, s.match_tier, s.route_d_subgroup,
                   s.selection_probability, s.anonymized_pair, s.drift_repeat) for s in samples)


__all__ = ["ANONYMIZED_PER_DAY", "CONTROL_PER_DAY", "DRIFT_PER_DAY", "MATCH_TIERS", "MAX_REQUESTS_PER_DAY",
           "PHASE_B_START", "PRIMARY_PER_DAY", "ROUTES", "ROUTE_D", "SELECTION_VERSION", "SUBGROUPS",
           "SUBGROUP_DEFINITION", "DaySelection", "SelectionError", "anonymized_count", "check_s0", "control_count",
           "drift_count", "route_d_subgroup", "select_day", "selection_key", "selection_signature"]
