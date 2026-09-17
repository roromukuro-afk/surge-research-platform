"""Technical indicators, as pure functions over a comparable price series.

Every function returns None rather than a number when there is not enough
history for it. That is deliberate: a 14-day ATR computed from nine bars is not
a short ATR, it is a different statistic, and a screening rule that fires on one
is firing on noise. The caller records which features were None and why, and the
warmup flag on the stored row says whether the longest window was satisfied.

Wilder's smoothing is used where Wilder defined the indicator - ATR, RSI, ADX -
rather than a simple moving average, because those are the values every other
tool will produce and a divergence here would be mistaken for signal.

Floats are used throughout. Prices arrive as Decimal and are converted at the
boundary: these are statistical measures, not money, and IEEE 754 is both
deterministic and what every reference implementation uses.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Number = float | int | None


def _clean(values: Sequence[Number]) -> list[float] | None:
    """Reject a window with a hole in it rather than interpolating one."""

    out: list[float] = []
    for value in values:
        if value is None:
            return None
        out.append(float(value))
    return out


def sma(values: Sequence[Number], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    tail = _clean(values[-window:])
    if tail is None:
        return None
    return sum(tail) / window


def ema(values: Sequence[Number], window: int) -> float | None:
    """Seeded with the SMA of the first ``window`` values, as is conventional."""

    if window <= 0 or len(values) < window:
        return None
    series = _clean(values)
    if series is None:
        return None
    multiplier = 2.0 / (window + 1)
    current = sum(series[:window]) / window
    for value in series[window:]:
        current = (value - current) * multiplier + current
    return current


def ema_series(values: Sequence[float], window: int) -> list[float | None]:
    """EMA at every point, for indicators that need the whole path (MACD)."""

    out: list[float | None] = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    multiplier = 2.0 / (window + 1)
    current = sum(values[:window]) / window
    out[window - 1] = current
    for index in range(window, len(values)):
        current = (values[index] - current) * multiplier + current
        out[index] = current
    return out


def pct_change(values: Sequence[Number], periods: int) -> float | None:
    """Return over ``periods`` bars, as a percentage."""

    if periods <= 0 or len(values) < periods + 1:
        return None
    latest = values[-1]
    earlier = values[-1 - periods]
    if latest is None or earlier is None or float(earlier) == 0.0:
        return None
    return (float(latest) / float(earlier) - 1.0) * 100.0


def rolling_max(values: Sequence[Number], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    tail = _clean(values[-window:])
    return None if tail is None else max(tail)


def rolling_min(values: Sequence[Number], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    tail = _clean(values[-window:])
    return None if tail is None else min(tail)


def bars_since_max(values: Sequence[Number], window: int) -> int | None:
    """How many bars back the window's high sits. 0 means it is today."""

    if window <= 0 or len(values) < window:
        return None
    tail = _clean(values[-window:])
    if tail is None:
        return None
    highest = max(tail)
    for offset, value in enumerate(reversed(tail)):
        if value == highest:
            return offset
    return None  # pragma: no cover - max is always present


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    """True range per bar. The first bar has no previous close, so it is H-L."""

    ranges = [highs[0] - lows[0]]
    for index in range(1, len(highs)):
        previous_close = closes[index - 1]
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - previous_close),
                abs(lows[index] - previous_close),
            )
        )
    return ranges


def _wilder_smooth(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < window:
        return out
    current = sum(values[:window]) / window
    out[window - 1] = current
    for index in range(window, len(values)):
        current = (current * (window - 1) + values[index]) / window
        out[index] = current
    return out


def atr(
    highs: Sequence[Number], lows: Sequence[Number], closes: Sequence[Number], window: int = 14
) -> float | None:
    h, low_values, c = _clean(highs), _clean(lows), _clean(closes)
    if h is None or low_values is None or c is None:
        return None
    if len(h) < window + 1:
        return None
    smoothed = _wilder_smooth(true_ranges(h, low_values, c)[1:], window)
    return smoothed[-1]


def rsi(closes: Sequence[Number], window: int = 14) -> float | None:
    series = _clean(closes)
    if series is None or len(series) < window + 1:
        return None

    gains = [max(series[i] - series[i - 1], 0.0) for i in range(1, len(series))]
    losses = [max(series[i - 1] - series[i], 0.0) for i in range(1, len(series))]

    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    for index in range(window, len(gains)):
        avg_gain = (avg_gain * (window - 1) + gains[index]) / window
        avg_loss = (avg_loss * (window - 1) + losses[index]) / window

    if avg_loss == 0.0:
        # Every bar in the window rose. 100 is the defined value, not a bug.
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    closes: Sequence[Number], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    """MACD line, signal line and histogram."""

    series = _clean(closes)
    if series is None or len(series) < slow:
        return (None, None, None)

    fast_line = ema_series(series, fast)
    slow_line = ema_series(series, slow)
    macd_points = [
        f - s for f, s in zip(fast_line, slow_line, strict=True) if f is not None and s is not None
    ]
    if not macd_points:
        return (None, None, None)

    macd_value = macd_points[-1]
    if len(macd_points) < signal:
        return (macd_value, None, None)

    signal_line = ema_series(macd_points, signal)
    signal_value = signal_line[-1]
    if signal_value is None:
        return (macd_value, None, None)
    return (macd_value, signal_value, macd_value - signal_value)


def adx_dmi(
    highs: Sequence[Number], lows: Sequence[Number], closes: Sequence[Number], window: int = 14
) -> tuple[float | None, float | None, float | None]:
    """ADX, +DI and -DI, by Wilder's definition.

    ADX needs roughly twice the window: the DX series has to exist before it can
    itself be smoothed. Below that it is None rather than a partial value.
    """

    h, low_values, c = _clean(highs), _clean(lows), _clean(closes)
    if h is None or low_values is None or c is None or len(h) < window + 1:
        return (None, None, None)

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index in range(1, len(h)):
        up = h[index] - h[index - 1]
        down = low_values[index - 1] - low_values[index]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    tr = true_ranges(h, low_values, c)[1:]
    smoothed_tr = _wilder_smooth(tr, window)
    smoothed_plus = _wilder_smooth(plus_dm, window)
    smoothed_minus = _wilder_smooth(minus_dm, window)

    dx_values: list[float] = []
    for index in range(len(tr)):
        trv, pdm, mdm = smoothed_tr[index], smoothed_plus[index], smoothed_minus[index]
        if trv is None or pdm is None or mdm is None or trv == 0:
            continue
        plus_di = 100.0 * pdm / trv
        minus_di = 100.0 * mdm / trv
        denominator = plus_di + minus_di
        dx_values.append(0.0 if denominator == 0 else 100.0 * abs(plus_di - minus_di) / denominator)

    latest_tr = smoothed_tr[-1]
    latest_plus = smoothed_plus[-1]
    latest_minus = smoothed_minus[-1]
    if latest_tr in (None, 0) or latest_plus is None or latest_minus is None:
        return (None, None, None)

    plus_di = 100.0 * latest_plus / latest_tr
    minus_di = 100.0 * latest_minus / latest_tr

    if len(dx_values) < window:
        return (None, plus_di, minus_di)
    adx_line = _wilder_smooth(dx_values, window)
    return (adx_line[-1], plus_di, minus_di)


def bollinger(
    closes: Sequence[Number], window: int = 20, deviations: float = 2.0
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Middle, upper, lower, width as a fraction of the middle, and %B."""

    series = _clean(closes)
    if series is None or len(series) < window:
        return (None, None, None, None, None)

    tail = series[-window:]
    middle = sum(tail) / window
    variance = sum((value - middle) ** 2 for value in tail) / window
    sigma = math.sqrt(variance)
    upper = middle + deviations * sigma
    lower = middle - deviations * sigma
    width = None if middle == 0 else (upper - lower) / middle
    span = upper - lower
    percent_b = None if span == 0 else (series[-1] - lower) / span
    return (middle, upper, lower, width, percent_b)


def realized_volatility(closes: Sequence[Number], window: int = 20, *, annualize: bool = False) -> float | None:
    """Sample standard deviation of log returns over the window."""

    series = _clean(closes)
    if series is None or len(series) < window + 1:
        return None
    tail = series[-(window + 1) :]
    returns: list[float] = []
    for index in range(1, len(tail)):
        if tail[index - 1] <= 0 or tail[index] <= 0:
            return None
        returns.append(math.log(tail[index] / tail[index - 1]))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sigma = math.sqrt(variance)
    return sigma * math.sqrt(252.0) if annualize else sigma


def relative_volume(volumes: Sequence[Number], window: int) -> float | None:
    """Today's volume against its own recent average, excluding today."""

    if len(volumes) < window + 1:
        return None
    latest = volumes[-1]
    baseline = _clean(volumes[-(window + 1) : -1])
    if latest is None or baseline is None:
        return None
    average = sum(baseline) / window
    if average == 0:
        return None
    return float(latest) / average


def candle_shape(
    open_: Number, high: Number, low: Number, close: Number
) -> tuple[float | None, float | None, float | None, float | None]:
    """Body, upper wick and lower wick as percentages of the range, plus CLV.

    CLV (close location value) is -1 at the low of the bar and +1 at the high.
    """

    values = _clean([open_, high, low, close])
    if values is None:
        return (None, None, None, None)
    o, h, low_value, c = values
    span = h - low_value
    if span <= 0:
        # A bar with no range - a limit move with a single print. Not an error,
        # but the shape ratios are undefined and must not be reported as zero.
        return (None, None, None, None)
    body = abs(c - o) / span * 100.0
    upper = (h - max(o, c)) / span * 100.0
    lower = (min(o, c) - low_value) / span * 100.0
    clv = ((c - low_value) - (h - c)) / span
    return (body, upper, lower, clv)


def consecutive_up_days(closes: Sequence[Number]) -> int | None:
    series = _clean(closes)
    if series is None or len(series) < 2:
        return None
    count = 0
    for index in range(len(series) - 1, 0, -1):
        if series[index] > series[index - 1]:
            count += 1
        else:
            break
    return count
