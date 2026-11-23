"""Feature context: every number a strategy may look at, precomputed once
over a candle series. Rule: the value at index i only uses information that
was public at the CLOSE of candle i (no look-ahead).

Derivatives data (open interest, funding) comes as (timestamp_ms, value)
points and is aligned to each candle as "the last value published at or
before that candle's close".
"""
from __future__ import annotations

from .indicators import Candle, atr, ema, rsi

# Indicators need this many candles before they are trustworthy. EMA200
# seeded on fewer than ~1000 candles is still dominated by its seed.
WARMUP = 250
SWING_K = 3  # a swing high/low is confirmed K candles after it happens


def align(points: list[tuple[int, float]] | None, close_times: list[int]) -> list[float | None]:
    out: list[float | None] = [None] * len(close_times)
    if not points:
        return out
    pts = sorted(points)
    j, last = 0, None
    for i, ct in enumerate(close_times):
        while j < len(pts) and pts[j][0] <= ct:
            last = pts[j][1]
            j += 1
        out[i] = last
    return out


class Ctx:
    def __init__(self, candles: list[Candle], symbol: str = "",
                 oi: list[tuple[int, float]] | None = None,
                 funding: list[tuple[int, float]] | None = None):
        self.symbol = symbol
        self.c = candles
        self.n = n = len(candles)
        self.o = [x.open for x in candles]
        self.h = [x.high for x in candles]
        self.l = [x.low for x in candles]
        self.cl = [x.close for x in candles]
        self.v = [x.volume for x in candles]
        self.t = [x.close_time for x in candles]
        self.taker_ratio = [(x.taker_buy / x.volume) if x.volume > 0 else 0.5 for x in candles]
        self.ema9 = ema(self.cl, 9)
        self.ema21 = ema(self.cl, 21)
        self.ema200 = ema(self.cl, 200)
        self.rsi14 = rsi(self.cl, 14)
        self.atr14 = atr(candles, 14)
        self.oi = align(oi, self.t)
        self.funding = align(funding, self.t)
        self.has_oi = any(x is not None for x in self.oi)
        # Most recent CONFIRMED swing high/low known at the close of candle i.
        self.swing_high: list[float | None] = [None] * n
        self.swing_low: list[float | None] = [None] * n
        k = SWING_K
        sh = sl = None
        for i in range(n):
            j = i - k
            if j >= k:
                win_h = self.h[j - k:j + k + 1]
                win_l = self.l[j - k:j + k + 1]
                if self.h[j] == max(win_h):
                    sh = self.h[j]
                if self.l[j] == min(win_l):
                    sl = self.l[j]
            self.swing_high[i], self.swing_low[i] = sh, sl
        self.cache: dict = {}

    def ready(self, i: int) -> bool:
        return i >= WARMUP and self.atr14[i] is not None and self.ema200[i] is not None
