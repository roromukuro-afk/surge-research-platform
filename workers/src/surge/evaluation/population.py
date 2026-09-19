"""Who is evaluated: the screening replay at S0 and the Primary / Control samples.

**Screening replay.** For a security and an S0, the as-traded history is cut at
S0 (``build_comparable_series(as_of=S0)``, which drops later bars and applies
only splits known by then), the feature row for S0 is computed and Routes A-H
run on it - the production functions, unchanged. A security is *eligible* when
it traded on S0 at no more than 3,000 yen as traded; it *passes* when any route
fires.

**Samples** (D-270). Primary = eligible and passing; Control = eligible and not
passing on the same S0, matched to a Primary sample by price band and
liquidity band. Primary : Control = 75 : 25. A security's S0s are at least 20
sessions apart, so outcome windows do not overlap. Everything is drawn from a
seeded generator, so the same inputs give the same population.
"""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from surge.evaluation.prices import History
from surge.evaluation.universe import ListedIssue
from surge.features.engine import FEATURE_VERSION, DailyFeatures, FeatureError, compute_features
from surge.market.series import ComparableSeries, SeriesError, build_comparable_series
from surge.routes.engine import ROUTE_VERSION, evaluate_routes

PRICE_LIMIT_JPY = Decimal("3000")
PRICE_BANDS = ((Decimal(0), Decimal(500)), (Decimal(500), Decimal(1000)),
               (Decimal(1000), Decimal(2000)), (Decimal(2000), Decimal(3000)))
MIN_SPACING_SESSIONS = 20
SCREENER_DEFINITION = (
    f"Routes A-H ({ROUTE_VERSION}) on {FEATURE_VERSION}, replayed point-in-time at S0; "
    "material routes M1-M6 and Stage 2 are not replayed"
)


@dataclass(frozen=True)
class Screen:
    code: str
    s0: date
    eligible: bool
    passed: bool
    close_as_traded: Decimal | None
    routes: list[str] = field(default_factory=list)
    route_evidence: dict = field(default_factory=dict)
    turnover_avg_20d: float | None = None
    series: ComparableSeries | None = field(default=None, repr=False, compare=False)
    features: DailyFeatures | None = field(default=None, repr=False, compare=False)
    reason: str | None = None


def screen(history: History, s0: date) -> Screen:
    """The screener's verdict on ``history`` at ``s0``, using nothing after ``s0``."""

    if history.bar_on(s0) is None:
        return Screen(history.code, s0, False, False, None, reason="no bar on S0")
    try:
        series = build_comparable_series(history.bars, history.actions, as_of=s0)
        features = compute_features(series)
    except (SeriesError, FeatureError) as exc:
        return Screen(history.code, s0, False, False, None, reason=str(exc))
    close = series.bars[-1].source.close
    if close is None or close > PRICE_LIMIT_JPY:
        return Screen(history.code, s0, False, False, close, reason="above 3,000 yen as traded")
    result = evaluate_routes(features, series=series)
    return Screen(
        code=history.code, s0=s0, eligible=True, passed=result.is_candidate, close_as_traded=close,
        routes=list(result.discovery_routes), route_evidence=result.route_evidence,
        turnover_avg_20d=features.turnover_avg_20d, series=series, features=features,
    )


def price_band(close: Decimal) -> str:
    for low, high in PRICE_BANDS:
        if low < close <= high:
            return f"{low}-{high}"
    return "above-3000"


def liquidity_bands(screens: list[Screen]) -> dict[str, str]:
    """Quartile of 20-day average turnover among the eligible, on one S0."""

    values = sorted(s.turnover_avg_20d for s in screens if s.eligible and s.turnover_avg_20d is not None)
    if not values:
        return {}
    cut = [values[int(len(values) * q / 4)] for q in (1, 2, 3)]
    bands = {}
    for s in screens:
        if s.eligible and s.turnover_avg_20d is not None:
            bands[s.code] = f"Q{1 + sum(s.turnover_avg_20d >= c for c in cut)}"
    return bands


@dataclass(frozen=True)
class Sample:
    sample_id: str
    cohort: str
    code: str
    name: str
    segment: str
    sector33: str
    size_category: str
    s0: date
    s0_close_as_traded: Decimal
    price_band: str
    liquidity_band: str
    routes: list[str]
    route_evidence: dict
    matched_to: str | None = None
    anonymized_pair: bool = False
    drift_repeat: bool = False
    #: Phase B (surge.evaluation.selection): the pre-registered Route D subgroup of a
    #: Primary sample, the probability it was drawn with, and how a Control was matched.
    route_d_subgroup: str | None = None
    selection_probability: float | None = None
    match_tier: str | None = None
    teacher_admissible: bool = False

    def row(self) -> dict:
        row = asdict(self)
        row["s0"] = self.s0.isoformat()
        row["s0_close_as_traded"] = str(self.s0_close_as_traded)
        return row


def sample_id(code: str, s0: date, cohort: str) -> str:
    return hashlib.sha256(f"{code}|{s0.isoformat()}|{cohort}".encode()).hexdigest()[:16]


def choose_s0_dates(sessions: list[date], start: date, end: date, per_month: int, rng: random.Random) -> list[date]:
    by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for day in sessions:
        if start <= day <= end:
            by_month[(day.year, day.month)].append(day)
    chosen = []
    for month in sorted(by_month):
        days = by_month[month]
        chosen.extend(sorted(rng.sample(days, min(per_month, len(days)))))
    return chosen


def draw_population(
    screens_by_date: dict[date, list[Screen]],
    issues: dict[str, ListedIssue],
    sessions: list[date],
    *,
    primary: int,
    control: int,
    anonymized: int,
    drift: int,
    seed: int,
) -> list[Sample]:
    """Primary samples spread over the months, Control samples matched to them."""

    rng = random.Random(seed)
    index = {day: i for i, day in enumerate(sessions)}
    taken: dict[str, list[int]] = defaultdict(list)

    def spaced(code: str, day: date) -> bool:
        return all(abs(index[day] - other) >= MIN_SPACING_SESSIONS for other in taken[code])

    def make(screen: Screen, cohort: str, band: str, matched_to: str | None = None) -> Sample:
        issue = issues[screen.code]
        taken[screen.code].append(index[screen.s0])
        return Sample(
            sample_id=sample_id(screen.code, screen.s0, cohort), cohort=cohort, code=screen.code,
            name=issue.name, segment=issue.segment, sector33=issue.sector33, size_category=issue.size_category,
            s0=screen.s0, s0_close_as_traded=screen.close_as_traded, price_band=price_band(screen.close_as_traded),
            liquidity_band=band, routes=screen.routes, route_evidence=screen.route_evidence, matched_to=matched_to,
        )

    months: dict[tuple[int, int], list[date]] = defaultdict(list)
    for day in sorted(screens_by_date):
        months[(day.year, day.month)].append(day)
    bands = {day: liquidity_bands(screens) for day, screens in screens_by_date.items()}

    # Primary: round-robin over months, a random passing security each time.
    pools = {
        month: [s for day in days for s in screens_by_date[day] if s.eligible and s.passed and s.code in issues]
        for month, days in months.items()
    }
    for pool in pools.values():
        rng.shuffle(pool)
    samples: list[Sample] = []
    order = sorted(pools)
    while len(samples) < primary and any(pools.values()):
        for month in order:
            if len(samples) >= primary:
                break
            pool = pools[month]
            while pool:
                candidate = pool.pop()
                if spaced(candidate.code, candidate.s0):
                    samples.append(make(candidate, "PRIMARY", bands[candidate.s0].get(candidate.code, "unknown")))
                    break

    # Control: matched to randomly chosen Primary samples on the same S0.
    anchors = rng.sample(samples, min(control, len(samples)))
    for anchor in anchors:
        day = anchor.s0
        candidates = [s for s in screens_by_date[day] if s.eligible and not s.passed and s.code in issues
                      and spaced(s.code, day)]
        rng.shuffle(candidates)

        def closeness(s: Screen, want=anchor, on=day) -> tuple[int, int]:
            same_price = price_band(s.close_as_traded) == want.price_band
            same_liquidity = bands[on].get(s.code) == want.liquidity_band
            return (0 if same_price and same_liquidity else 1 if same_price else 2 if same_liquidity else 3, 0)

        if candidates:
            best = min(candidates, key=closeness)
            samples.append(make(best, "CONTROL", bands[day].get(best.code, "unknown"), matched_to=anchor.sample_id))

    # The two sensitivity subsets: drawn once, disjoint, recorded on the sample.
    ids = [s.sample_id for s in samples]
    rng.shuffle(ids)
    anon_ids, drift_ids = set(ids[:anonymized]), set(ids[anonymized:anonymized + drift])
    return [
        Sample(**{**asdict(s), "anonymized_pair": s.sample_id in anon_ids, "drift_repeat": s.sample_id in drift_ids})
        for s in samples
    ]


def reduce_population(samples: list[Sample], *, primary: int, control: int, anonymized: int, drift: int,
                      seed: str) -> list[Sample]:
    """A seeded subset of a drawn population (D-274): ``primary`` and ``control`` samples, each cohort sampled apart.

    The choice reads nothing but the cohort and the sample id - never an
    outcome, a price or an answer - so it cannot favour a result. The
    anonymized and drift subsets are drawn again, disjoint, from the chosen
    samples; the original flags are cleared. The original order is kept.
    """

    rng = random.Random(seed)
    chosen = []
    for cohort, count in (("PRIMARY", primary), ("CONTROL", control)):
        pool = sorted((s for s in samples if s.cohort == cohort), key=lambda s: s.sample_id)
        if len(pool) < count:
            raise ValueError(f"{len(pool)} {cohort} samples, {count} wanted")
        chosen += rng.sample(pool, count)
    ids = sorted(s.sample_id for s in chosen)
    rng.shuffle(ids)
    anon_ids, drift_ids = set(ids[:anonymized]), set(ids[anonymized:anonymized + drift])
    keep = {s.sample_id for s in chosen}
    return [
        Sample(**{**asdict(s), "anonymized_pair": s.sample_id in anon_ids, "drift_repeat": s.sample_id in drift_ids})
        for s in samples if s.sample_id in keep
    ]


__all__ = ["MIN_SPACING_SESSIONS", "PRICE_LIMIT_JPY", "SCREENER_DEFINITION", "Sample", "Screen", "choose_s0_dates",
           "draw_population", "liquidity_bands", "price_band", "reduce_population", "sample_id", "screen"]
