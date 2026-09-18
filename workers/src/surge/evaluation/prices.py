"""Yahoo daily history for the evaluation, restored to prices as traded.

Yahoo's daily chart restates every bar before a split into today's share count
(measured 2026-09-18). The screening replay and the 3,000 yen test need the
prices as traded, and ``build_comparable_series`` refuses anything else, so each
bar is multiplied back by the splits after its date (volume divided), and the
splits themselves become ``CanonicalAction`` rows. The series built from these
for an as-of date then applies only the splits known by that date - the same
point-in-time rule the production pipeline follows.

Turnover is not in Yahoo's data. It is approximated as close x volume, which
route F uses; the approximation is recorded with the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from surge.market.models import CanonicalAction, CanonicalBar, CorporateActionType, PriceBasis
from surge.providers.yahoo_finance import JST, YahooSession, as_traded_price, parse_splits, split_factor_after

PROVIDER_ID = "yahoo_finance"
DATASET_KEY = "YAHOO_JP_DAILY_AS_TRADED"
TURNOVER_BASIS = "APPROXIMATED_CLOSE_X_VOLUME"


class PriceHistoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class History:
    code: str
    symbol: str
    bars: list[CanonicalBar]
    actions: list[CanonicalAction]
    fetched_at: datetime
    notes: list[str] = field(default_factory=list)

    def bar_on(self, day: date) -> CanonicalBar | None:
        for bar in self.bars:
            if bar.trade_date == day:
                return bar
        return None


def history_from_chart(code: str, symbol: str, result: dict, *, fetched_at: datetime) -> History:
    """One chart result (``events=split``) as as-traded bars and split actions."""

    meta = result.get("meta") or {}
    if meta.get("currency") not in (None, "JPY"):
        raise PriceHistoryError(f"{symbol}: currency {meta.get('currency')!r}, not JPY")
    splits = parse_splits(result)
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, stamp in enumerate(result.get("timestamp") or []):
        close = (quote.get("close") or [None])[i] if i < len(quote.get("close") or []) else None
        if close is None:
            continue
        day = datetime.fromtimestamp(stamp, JST).date()

        def traded(values, index=i, on=day):
            value = values[index] if values and index < len(values) else None
            return None if value is None else as_traded_price(value, on, splits)

        volume_raw = (quote.get("volume") or [None])[i] if i < len(quote.get("volume") or []) else None
        volume = (
            None if volume_raw is None
            else (Decimal(str(volume_raw)) / split_factor_after(day, splits)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
        close_traded = as_traded_price(close, day, splits)
        bars.append(CanonicalBar(
            provider_id=PROVIDER_ID,
            dataset_key=DATASET_KEY,
            market_code="JP",
            native_symbol=code,
            trade_date=day,
            currency="JPY",
            open=traded(quote.get("open")),
            high=traded(quote.get("high")),
            low=traded(quote.get("low")),
            close=close_traded,
            volume=volume,
            turnover=None if volume is None else close_traded * volume,
            open_basis=PriceBasis.RAW,
            high_basis=PriceBasis.RAW,
            low_basis=PriceBasis.RAW,
            close_basis=PriceBasis.RAW,
            volume_basis=PriceBasis.RAW,
            observed_at=fetched_at,
            available_at=fetched_at,
        ))
    actions = [
        CanonicalAction(
            provider_id=PROVIDER_ID,
            dataset_key=DATASET_KEY,
            market_code="JP",
            native_symbol=code,
            action_type=CorporateActionType.SPLIT if s.share_multiplier >= 1 else CorporateActionType.REVERSE_SPLIT,
            ex_date=s.ex_date,
            split_from=s.denominator,
            split_to=s.numerator,
            observed_at=fetched_at,
            available_at=fetched_at,
        )
        for s in splits
    ]
    notes = [f"{len(splits)} split(s) undone to as-traded prices"] if splits else []
    return History(code=code, symbol=symbol, bars=bars, actions=actions, fetched_at=fetched_at, notes=notes)


def fetch_chart(symbol: str, start: date, *, session: YahooSession, now: datetime) -> dict:
    """The chart result from ``start`` to now, splits included, as Yahoo returned it.

    To now, not to the end of the period of interest: every split since has to
    be seen to undo it.
    """

    period1 = int(datetime(start.year, start.month, start.day, tzinfo=JST).timestamp())
    data = session.chart(symbol, {"interval": "1d", "period1": str(period1), "period2": str(int(now.timestamp())),
                                  "events": "split"})
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        raise PriceHistoryError(f"no chart for {symbol}")
    return results[0]


def fetch_history(code: str, symbol: str, start: date, *, session: YahooSession, now: datetime | None = None) -> History:
    now = now or datetime.now(UTC)
    return history_from_chart(code, symbol, fetch_chart(symbol, start, session=session, now=now), fetched_at=now)


def trading_sessions(histories: list[History], *, min_share: float = 0.3) -> list[date]:
    """The days the market traded: dates on which enough of the sampled symbols have a bar.

    A calendar is not guessed (D-142). A date with no bars at all was not a
    session; a date where only a handful of symbols printed is treated the same,
    so one stray bar cannot invent a session.
    """

    counts: dict[date, int] = {}
    for history in histories:
        for bar in history.bars:
            counts[bar.trade_date] = counts.get(bar.trade_date, 0) + 1
    threshold = max(1, int(len(histories) * min_share))
    return sorted(day for day, n in counts.items() if n >= threshold)


__all__ = ["DATASET_KEY", "PROVIDER_ID", "TURNOVER_BASIS", "History", "PriceHistoryError", "fetch_chart",
           "fetch_history", "history_from_chart", "trading_sessions"]
