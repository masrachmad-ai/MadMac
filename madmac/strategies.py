"""Candidate scalping rules. NONE of these is assumed profitable.
Each one must pass the Lab (real data, fees, slippage, out-of-sample)
before Shield lets it near an order.

A strategy looks at CLOSED candles only and returns a Signal or None.
Entry is the next candle's open (≈ current price); stop and target are
ATR-based so they scale with volatility.
"""
from __future__ import annotations

from dataclasses import dataclass

from .indicators import Candle, atr, ema, rsi


@dataclass(frozen=True)
class Signal:
    strategy: str
    side: str          # "LONG" or "SHORT"
    entry: float       # reference price (last close)
    stop: float
    target: float
    reason: str        # plain-language reason, goes to Telegram + journal
    candle_time: int   # close_time of the candle that triggered it (ms UTC)

    @property
    def stop_distance_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry * 100


def _mk(name, side, c: Candle, a: float, stop_atr: float, rr: float, reason: str) -> Signal:
    if side == "LONG":
        stop, target = c.close - stop_atr * a, c.close + stop_atr * a * rr
    else:
        stop, target = c.close + stop_atr * a, c.close - stop_atr * a * rr
    return Signal(name, side, c.close, round(stop, 1), round(target, 1), reason, c.close_time)


def ema_pullback(candles: list[Candle]) -> Signal | None:
    """Trend-following: price above EMA200 (uptrend), pulls back to EMA21,
    then closes back above EMA9. Mirror for shorts."""
    if len(candles) < 210:
        return None
    closes = [c.close for c in candles]
    e9, e21, e200 = ema(closes, 9)[-1], ema(closes, 21)[-1], ema(closes, 200)[-1]
    a = atr(candles, 14)[-1]
    c, p = candles[-1], candles[-2]
    if None in (e9, e21, e200, a):
        return None
    if c.close > e200 and e9 > e21 and p.low <= e21 and c.close > e9 and c.close > c.open:
        return _mk("ema_pullback", "LONG", c, a, 1.5, 1.5,
                   "Tren naik (harga > EMA200), harga sempat turun ke EMA21 lalu ditutup kembali di atas EMA9.")
    if c.close < e200 and e9 < e21 and p.high >= e21 and c.close < e9 and c.close < c.open:
        return _mk("ema_pullback", "SHORT", c, a, 1.5, 1.5,
                   "Tren turun (harga < EMA200), harga sempat naik ke EMA21 lalu ditutup kembali di bawah EMA9.")
    return None


def rsi_reversion(candles: list[Candle]) -> Signal | None:
    """Mean reversion WITH the trend: in an uptrend (above EMA200) buy when
    RSI(14) dips below 30 then turns up. Mirror for shorts."""
    if len(candles) < 210:
        return None
    closes = [c.close for c in candles]
    r = rsi(closes, 14)
    e200 = ema(closes, 200)[-1]
    a = atr(candles, 14)[-1]
    c = candles[-1]
    if None in (r[-1], r[-2], e200, a):
        return None
    if c.close > e200 and r[-2] < 30 <= r[-1]:
        return _mk("rsi_reversion", "LONG", c, a, 1.2, 1.2,
                   f"Tren naik, RSI sempat oversold ({r[-2]:.0f}) lalu naik kembali ke {r[-1]:.0f}.")
    if c.close < e200 and r[-2] > 70 >= r[-1]:
        return _mk("rsi_reversion", "SHORT", c, a, 1.2, 1.2,
                   f"Tren turun, RSI sempat overbought ({r[-2]:.0f}) lalu turun kembali ke {r[-1]:.0f}.")
    return None


def range_breakout(candles: list[Candle]) -> Signal | None:
    """Breakout: close beyond the 20-candle high/low with volume at least
    1.5x the 20-candle average."""
    if len(candles) < 40:
        return None
    look = candles[-21:-1]
    hi, lo = max(x.high for x in look), min(x.low for x in look)
    avg_vol = sum(x.volume for x in look) / len(look)
    a = atr(candles, 14)[-1]
    c = candles[-1]
    if a is None or avg_vol <= 0:
        return None
    if c.close > hi and c.volume >= 1.5 * avg_vol:
        return _mk("range_breakout", "LONG", c, a, 1.0, 2.0,
                   f"Harga tembus ke atas level tertinggi 20 candle ({hi:,.1f}) dengan volume {c.volume / avg_vol:.1f}x rata-rata.")
    if c.close < lo and c.volume >= 1.5 * avg_vol:
        return _mk("range_breakout", "SHORT", c, a, 1.0, 2.0,
                   f"Harga tembus ke bawah level terendah 20 candle ({lo:,.1f}) dengan volume {c.volume / avg_vol:.1f}x rata-rata.")
    return None


STRATEGIES = {
    "ema_pullback": ema_pullback,
    "rsi_reversion": rsi_reversion,
    "range_breakout": range_breakout,
}
