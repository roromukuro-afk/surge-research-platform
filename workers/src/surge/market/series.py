"""Turning stored bars into a series you can actually compare across a split.

A two-for-one split halves the price overnight. Left alone, a feature engine
reads that as a 50% fall, and later an outcome engine reads the reverse case as
a +100% gain. Neither happened. So before anything measures a return, the raw
prices are restated into one share count.

Two rules make that restatement honest:

*Only splits the as-of date could have known are applied.* A series built for
2024-03-01 must not know about a split in 2024-06. Vendor "adjusted" columns
violate this by construction - they are recomputed backwards from today - which
is why this module builds its own series from raw prices rather than using them.

*A volume the vendor already adjusted is not silently adjusted again.* EODHD
restates volume to the present; applying our factor on top would double-count,
and un-applying it needs splits that happened after the as-of date, which is
future information. Where that arises the volume is dropped and said to be
dropped, rather than quietly wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from surge.market.models import (
    SHARE_COUNT_ACTIONS,
    CanonicalAction,
    CanonicalBar,
    PriceBasis,
)

SERIES_BASIS_SPLIT_ADJUSTED_TO_AS_OF = "SPLIT_ADJUSTED_TO_AS_OF"
SERIES_BASIS_RAW = "RAW_UNADJUSTED"


class SeriesError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComparableBar:
    """One bar restated into the as-of date's share count."""

    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    turnover: Decimal | None
    # The cumulative share multiplier applied from this date to the as-of date.
    # 1 means nothing happened after this bar.
    share_multiplier: Decimal
    volume_is_comparable: bool
    source: CanonicalBar


@dataclass(frozen=True)
class ComparableSeries:
    native_symbol: str
    market_code: str
    provider_id: str
    as_of: date
    basis: str
    bars: list[ComparableBar]
    applied_actions: list[CanonicalAction]
    unusable_actions: list[CanonicalAction]
    volume_comparable: bool
    notes: list[str]

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def closes(self) -> list[Decimal | None]:
        return [bar.close for bar in self.bars]

    @property
    def is_broken(self) -> bool:
        """True when a share-count action could not be read.

        A series with an unreadable split is not a series with one bad day: every
        return spanning that date is wrong. Callers must refuse to derive
        features rather than produce plausible numbers.
        """

        return bool(self.unusable_actions)


def build_comparable_series(
    bars: list[CanonicalBar],
    actions: list[CanonicalAction],
    *,
    as_of: date,
    require_raw: bool = True,
) -> ComparableSeries:
    """Restate ``bars`` into the share count in force on ``as_of``.

    ``actions`` may contain anything; only share-count actions with an ex-date
    at or before ``as_of`` are applied, and only those strictly after a bar's own
    date affect that bar.
    """

    if not bars:
        raise SeriesError("cannot build a series from no bars")

    notes: list[str] = []

    # Bars after the as-of date are dropped, not trusted to have been filtered
    # upstream. A feature row is named by its last bar, so one stray future bar
    # would silently move the whole calculation forward in time - the exact leak
    # the as-of argument exists to prevent.
    ordered = sorted((b for b in bars if b.trade_date <= as_of), key=lambda b: b.trade_date)
    dropped = len(bars) - len(ordered)
    if dropped:
        notes.append(f"dropped {dropped} bar(s) dated after {as_of}")
    if not ordered:
        raise SeriesError(f"no bars at or before {as_of}")

    symbols = {b.native_symbol for b in ordered}
    if len(symbols) != 1:
        raise SeriesError(f"a series must be one security, got {sorted(symbols)}")
    if require_raw:
        non_raw = [b for b in ordered if not b.is_ohlc_raw()]
        if non_raw:
            raise SeriesError(
                f"{len(non_raw)} of {len(ordered)} bars are not raw as traded; "
                "a comparable series must be built from unadjusted prices"
            )

    relevant = [
        action
        for action in actions
        if action.action_type in SHARE_COUNT_ACTIONS and action.ex_date <= as_of
    ]
    applied = [a for a in relevant if a.share_multiplier is not None]
    unusable = [a for a in relevant if a.share_multiplier is None]
    if unusable:
        notes.append(
            f"{len(unusable)} share-count action(s) had no readable ratio; "
            "every return spanning those dates is unreliable"
        )

    # Volume: the vendor may already have restated it, and to a date we did not
    # choose. Usable only when no split sits between our as-of date and now.
    vendor_adjusted_volume = any(b.volume_basis is PriceBasis.SPLIT_ADJUSTED for b in ordered)
    actions_after_as_of = [
        a for a in actions if a.action_type in SHARE_COUNT_ACTIONS and a.ex_date > as_of
    ]
    volume_comparable = True
    if vendor_adjusted_volume:
        if actions_after_as_of:
            volume_comparable = False
            notes.append(
                "volume was already split-adjusted by the provider to its own fetch date, and "
                f"{len(actions_after_as_of)} split(s) occurred after {as_of}; the volume cannot be "
                "restated to the as-of date without future information and is dropped"
            )
        else:
            notes.append(
                "volume was split-adjusted by the provider; no split occurred after the as-of date, "
                "so it is already on the as-of share count"
            )

    comparable: list[ComparableBar] = []
    for bar in ordered:
        multiplier = Decimal(1)
        for action in applied:
            if action.ex_date > bar.trade_date:
                multiplier *= action.share_multiplier  # type: ignore[operator]

        def restate(value: Decimal | None, m: Decimal = multiplier) -> Decimal | None:
            return None if value is None else value / m

        if bar.volume is None or not volume_comparable:
            volume: Decimal | None = None
        elif bar.volume_basis is PriceBasis.SPLIT_ADJUSTED:
            # already on the as-of share count, per the check above
            volume = bar.volume
        else:
            volume = bar.volume * multiplier

        comparable.append(
            ComparableBar(
                trade_date=bar.trade_date,
                open=restate(bar.open),
                high=restate(bar.high),
                low=restate(bar.low),
                close=restate(bar.close),
                volume=volume,
                # Turnover is money, not shares: a split does not change it.
                turnover=bar.turnover,
                share_multiplier=multiplier,
                volume_is_comparable=volume is not None,
                source=bar,
            )
        )

    basis = SERIES_BASIS_SPLIT_ADJUSTED_TO_AS_OF if applied else SERIES_BASIS_RAW
    if not applied and not unusable:
        notes.append("no share-count action in range; the raw series is already comparable")

    return ComparableSeries(
        native_symbol=ordered[0].native_symbol,
        market_code=ordered[0].market_code,
        provider_id=ordered[0].provider_id,
        as_of=as_of,
        basis=basis,
        bars=comparable,
        applied_actions=applied,
        unusable_actions=unusable,
        volume_comparable=volume_comparable,
        notes=notes,
    )


def latest_price(bars: list[CanonicalBar], *, as_of: date) -> CanonicalBar | None:
    """The most recent bar with a close at or before ``as_of``.

    Used by the eligibility filter, which must never reach past its own cutoff
    and must be able to say how old the answer is.
    """

    candidates = [b for b in bars if b.trade_date <= as_of and b.close is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda b: b.trade_date)
