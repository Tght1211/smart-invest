"""Technical-indicator helpers for Smart-Invest Phase 3.

Pure stdlib. Each function accepts a list of NAVs in chronological order
(oldest → newest) and returns a single scalar (or tuple), or None when
there isn't enough data.

These signals are attached to decision-packet `actions[].context` so users
and Claude can see them. Phase 3 does NOT use them inside any rule —
that's P5 work, gated on backtest evidence per the design spec.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


def _ema(values: List[float], period: int) -> List[float]:
    """Exponential moving average. Returns a list of same length as input
    (first `period-1` entries use simple averages as warmup)."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = []
    for i, v in enumerate(values):
        if i == 0:
            out.append(v)
        elif i < period - 1:
            out.append(sum(values[: i + 1]) / (i + 1))
        elif i == period - 1:
            out.append(sum(values[:period]) / period)
        else:
            out.append(v * k + out[-1] * (1 - k))
    return out


def compute_rsi(navs: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI on close-to-close changes.

    Returns 0-100, or None if fewer than `period+1` NAVs.
    Special case: flat series (no movement) → 50.0.
    """
    if len(navs) < period + 1:
        return None
    deltas = [navs[i] - navs[i - 1] for i in range(1, len(navs))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    # Wilder's smoothing — start with simple average of last `period` then EMA-like
    recent_gains = gains[-period:]
    recent_losses = losses[-period:]
    avg_g = sum(recent_gains) / period
    avg_l = sum(recent_losses) / period
    if avg_g == 0 and avg_l == 0:
        return 50.0
    if avg_l == 0:
        return 100.0
    if avg_g == 0:
        return 0.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def compute_macd(
    navs: List[float], fast: int = 12, slow: int = 26, signal: int = 9,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Standard MACD on a NAV series.

    Returns (macd_line, signal_line, histogram) — each is the most recent value.
    Needs at least slow+signal NAVs; returns (None, None, None) otherwise.
    """
    if len(navs) < slow + signal:
        return None, None, None
    fast_ema = _ema(navs, fast)
    slow_ema = _ema(navs, slow)
    macd_series = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_series = _ema(macd_series, signal)
    macd_val   = macd_series[-1]
    signal_val = signal_series[-1]
    hist       = macd_val - signal_val
    return round(macd_val, 6), round(signal_val, 6), round(hist, 6)


def compute_ma_slope(
    navs: List[float], window: int = 20, lookback: int = 5,
) -> Optional[float]:
    """Slope of the `window`-day moving average over the last `lookback` days.

    Returns the average daily % change of the MA, e.g. +0.003 means MA
    rose ~0.3% per day on average. None if not enough data.
    """
    if len(navs) < window + lookback:
        return None
    ma = []
    for i in range(window - 1, len(navs)):
        ma.append(sum(navs[i - window + 1 : i + 1]) / window)
    # ma has len = len(navs) - window + 1
    if len(ma) < lookback + 1:
        return None
    recent = ma[-(lookback + 1):]
    pct_changes = [
        (recent[i] - recent[i - 1]) / recent[i - 1]
        for i in range(1, len(recent))
        if recent[i - 1]
    ]
    if not pct_changes:
        return None
    return round(sum(pct_changes) / len(pct_changes), 6)


def compute_breakout(navs: List[float], lookback: int = 20) -> Optional[bool]:
    """True if the latest NAV strictly exceeds the previous `lookback`-day high."""
    if len(navs) < lookback + 1:
        return None
    latest = navs[-1]
    prior_window = navs[-(lookback + 1):-1]
    return latest > max(prior_window)


def compute_ma_state(
    closes: List[float], window: int = 200, buffer: float = 0.01,
) -> Optional[dict]:
    """长均线趋势状态（Faber 200日线过滤，P5）。

    Returns {ma, gap_pct, below_days, above} or None if fewer than `window` closes.
    - gap_pct: (最新收盘 - MA) / MA
    - below_days: 连续多少天收盘 < 当日MA*(1-buffer)（从最新往回数）
    - above: 最新收盘 >= 当日 MA
    """
    n = len(closes)
    if n < window:
        return None
    # 滚动 MA（只算能算的尾部区间，避免 O(n*window)）
    ma_series = []
    s = sum(closes[:window])
    ma_series.append(s / window)
    for i in range(window, n):
        s += closes[i] - closes[i - window]
        ma_series.append(s / window)
    # ma_series[j] 对应 closes[window-1+j]
    last_ma = ma_series[-1]
    last = closes[-1]
    below_days = 0
    for j in range(len(ma_series) - 1, -1, -1):
        c = closes[window - 1 + j]
        if c < ma_series[j] * (1 - buffer):
            below_days += 1
        else:
            break
    return {
        "ma": round(last_ma, 6),
        "gap_pct": round((last - last_ma) / last_ma, 6) if last_ma else 0.0,
        "below_days": below_days,
        "above": last >= last_ma,
    }


def attach_signals(navs: List[float]) -> dict:
    """Convenience wrapper — returns a dict of all signals (with None for insufficient data)."""
    return {
        "rsi_14": compute_rsi(navs, period=14),
        "macd_hist": compute_macd(navs)[2],
        "ma20_slope": compute_ma_slope(navs, window=20, lookback=5),
        "breakout_20d": compute_breakout(navs, lookback=20),
    }
