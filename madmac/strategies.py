"""Strategy rules. NONE of these is assumed profitable. Each (strategy, coin)
pair must pass the Lab before Shield lets it near an order.

API: scan(ctx, params) -> list[Signal | None], one slot per candle. A signal
at index i is decided at the CLOSE of candle i and can only be traded from
candle i+1 onward.

Three groups:
  * classic      : ema_pullback, rsi_reversion, range_breakout
  * smart money  : smc_sweep (liquidity sweep + reclaim), fvg_retest
                   (fair value gap, first retest)
  * derivatives  : bos_oi (break of structure confirmed by rising open
                   interest and aggressive taker flow). smc_sweep also uses
                   open interest and funding as filters.
"""
from __future__ import annotations

from dataclasses import dataclass

from .features import Ctx


@dataclass(frozen=True)
class Signal:
    strategy: str
    side: str          # "LONG" or "SHORT"
    entry: float       # reference price (close of signal candle)
    stop: float
    target: float
    reason: str        # plain-language reason, goes to Telegram + journal
    candle_time: int   # close_time of the signal candle (ms UTC)
    symbol: str = ""
    max_hold: int = 36  # candles; then exit at market (time stop)

    @property
    def stop_distance_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry * 100


def _sig(ctx: Ctx, i: int, name: str, side: str, stop: float, rr: float, reason: str, hold: int) -> Signal | None:
    e = ctx.cl[i]
    risk = (e - stop) if side == "LONG" else (stop - e)
    if risk <= 0:
        return None
    target = e + rr * risk if side == "LONG" else e - rr * risk
    return Signal(name, side, e, stop, target, reason, ctx.t[i], ctx.symbol, hold)


def _fmt(x: float) -> str:
    return f"{x:,.6g}"


# ------------------------------------------------------------------ classic
def ema_pullback(ctx: Ctx, p: dict) -> list:
    out = [None] * ctx.n
    for i in range(1, ctx.n):
        if not ctx.ready(i):
            continue
        a, c = ctx.atr14[i], ctx.cl[i]
        e9, e21, e200 = ctx.ema9[i], ctx.ema21[i], ctx.ema200[i]
        if c > e200 and e9 > e21 and ctx.l[i - 1] <= e21 and c > e9 and c > ctx.o[i]:
            out[i] = _sig(ctx, i, "ema_pullback", "LONG", c - p["stop_atr"] * a, p["rr"],
                          "Tren naik (> EMA200), turun ke EMA21 lalu ditutup lagi di atas EMA9.", p["hold"])
        elif c < e200 and e9 < e21 and ctx.h[i - 1] >= e21 and c < e9 and c < ctx.o[i]:
            out[i] = _sig(ctx, i, "ema_pullback", "SHORT", c + p["stop_atr"] * a, p["rr"],
                          "Tren turun (< EMA200), naik ke EMA21 lalu ditutup lagi di bawah EMA9.", p["hold"])
    return out


def rsi_reversion(ctx: Ctx, p: dict) -> list:
    out = [None] * ctx.n
    lo, hi = p["rsi_low"], 100 - p["rsi_low"]
    for i in range(1, ctx.n):
        if not ctx.ready(i) or ctx.rsi14[i - 1] is None:
            continue
        a, c, r0, r1 = ctx.atr14[i], ctx.cl[i], ctx.rsi14[i - 1], ctx.rsi14[i]
        if c > ctx.ema200[i] and r0 < lo <= r1:
            out[i] = _sig(ctx, i, "rsi_reversion", "LONG", c - p["stop_atr"] * a, p["rr"],
                          f"Tren naik, RSI oversold ({r0:.0f}) lalu berbalik ke {r1:.0f}.", p["hold"])
        elif c < ctx.ema200[i] and r0 > hi >= r1:
            out[i] = _sig(ctx, i, "rsi_reversion", "SHORT", c + p["stop_atr"] * a, p["rr"],
                          f"Tren turun, RSI overbought ({r0:.0f}) lalu berbalik ke {r1:.0f}.", p["hold"])
    return out


def range_breakout(ctx: Ctx, p: dict) -> list:
    out = [None] * ctx.n
    lb = p["lookback"]
    for i in range(lb + 1, ctx.n):
        if not ctx.ready(i):
            continue
        hi = max(ctx.h[i - lb:i])
        lo = min(ctx.l[i - lb:i])
        av = sum(ctx.v[i - lb:i]) / lb
        a, c = ctx.atr14[i], ctx.cl[i]
        if av <= 0 or ctx.v[i] < p["vol_mult"] * av:
            continue
        if c > hi:
            out[i] = _sig(ctx, i, "range_breakout", "LONG", c - p["stop_atr"] * a, p["rr"],
                          f"Tembus ke atas high {lb} candle ({_fmt(hi)}), volume {ctx.v[i] / av:.1f}x.", p["hold"])
        elif c < lo:
            out[i] = _sig(ctx, i, "range_breakout", "SHORT", c + p["stop_atr"] * a, p["rr"],
                          f"Tembus ke bawah low {lb} candle ({_fmt(lo)}), volume {ctx.v[i] / av:.1f}x.", p["hold"])
    return out


# ------------------------------------------------------------------ smart money
def smc_sweep(ctx: Ctx, p: dict) -> list:
    """Liquidity sweep: price spikes beyond the last confirmed swing low
    (where stop-losses rest), then closes back above it with buyers in
    control. Optional derivatives filters:
      * use_oi: open interest FELL over the sweep (stops/liquidations flushed)
      * funding_max: skip longs when funding shows the crowd is already long
    Mirror for shorts above swing highs."""
    out = [None] * ctx.n
    lb = p["oi_lookback"]
    for i in range(lb + 1, ctx.n):
        if not ctx.ready(i):
            continue
        a, c = ctx.atr14[i], ctx.cl[i]
        sl, sh = ctx.swing_low[i - 1], ctx.swing_high[i - 1]
        oi_now, oi_then = ctx.oi[i], ctx.oi[i - lb]
        f = ctx.funding[i]
        if p["use_oi"] and (oi_now is None or oi_then is None):
            continue
        oi_drop = (not p["use_oi"]) or oi_now < oi_then
        if (sl is not None and ctx.l[i] < sl - p["min_sweep_atr"] * a and c > sl and c > ctx.o[i]
                and ctx.taker_ratio[i] > 0.5 and oi_drop and (f is None or f < p["funding_max"])):
            out[i] = _sig(ctx, i, "smc_sweep", "LONG", ctx.l[i] - p["stop_buf_atr"] * a, p["rr"],
                          f"Sapu likuiditas di bawah swing low {_fmt(sl)} lalu ditutup kembali di atasnya; "
                          f"pembeli agresif {ctx.taker_ratio[i]:.0%}" + ("; open interest turun (stop tersapu)" if p["use_oi"] else "") + ".",
                          p["hold"])
        elif (sh is not None and ctx.h[i] > sh + p["min_sweep_atr"] * a and c < sh and c < ctx.o[i]
              and ctx.taker_ratio[i] < 0.5 and oi_drop and (f is None or f > -p["funding_max"])):
            out[i] = _sig(ctx, i, "smc_sweep", "SHORT", ctx.h[i] + p["stop_buf_atr"] * a, p["rr"],
                          f"Sapu likuiditas di atas swing high {_fmt(sh)} lalu ditutup kembali di bawahnya; "
                          f"penjual agresif {1 - ctx.taker_ratio[i]:.0%}" + ("; open interest turun (stop tersapu)" if p["use_oi"] else "") + ".",
                          p["hold"])
    return out


def fvg_retest(ctx: Ctx, p: dict) -> list:
    """Fair value gap: a 3-candle imbalance (candle 3's low above candle 1's
    high). The FIRST time price returns into the gap, in the direction of the
    trend (EMA200), and closes back out of it, enter. Mirror for shorts."""
    out = [None] * ctx.n
    bulls: list[list] = []   # [bottom, top, born_i, touched]
    bears: list[list] = []
    for i in range(2, ctx.n):
        a = ctx.atr14[i]
        c = ctx.cl[i]
        if ctx.ready(i):
            for g in bulls:
                bottom, top, born, touched = g
                if not touched and i - born <= p["max_age"] and ctx.l[i] <= top and c > bottom \
                        and c > ctx.o[i] and c > ctx.ema200[i] and out[i] is None:
                    out[i] = _sig(ctx, i, "fvg_retest", "LONG", bottom - p["stop_buf_atr"] * a, p["rr"],
                                  f"Retest pertama fair value gap bullish {_fmt(bottom)}–{_fmt(top)} dalam tren naik.",
                                  p["hold"])
            for g in bears:
                bottom, top, born, touched = g
                if not touched and i - born <= p["max_age"] and ctx.h[i] >= bottom and c < top \
                        and c < ctx.o[i] and c < ctx.ema200[i] and out[i] is None:
                    out[i] = _sig(ctx, i, "fvg_retest", "SHORT", top + p["stop_buf_atr"] * a, p["rr"],
                                  f"Retest pertama fair value gap bearish {_fmt(bottom)}–{_fmt(top)} dalam tren turun.",
                                  p["hold"])
        for g in bulls:
            if ctx.l[i] <= g[1]:
                g[3] = True
        for g in bears:
            if ctx.h[i] >= g[0]:
                g[3] = True
        bulls = [g for g in bulls if not g[3] and i - g[2] < p["max_age"] and ctx.l[i] > g[0]]
        bears = [g for g in bears if not g[3] and i - g[2] < p["max_age"] and ctx.h[i] < g[1]]
        if a:
            if ctx.l[i] - ctx.h[i - 2] >= p["min_gap_atr"] * a:
                bulls.append([ctx.h[i - 2], ctx.l[i], i, False])
            if ctx.l[i - 2] - ctx.h[i] >= p["min_gap_atr"] * a:
                bears.append([ctx.h[i], ctx.l[i - 2], i, False])
    return out


# ------------------------------------------------------------------ derivatives
def bos_oi(ctx: Ctx, p: dict) -> list:
    """Break of structure WITH new money: close breaks the last confirmed
    swing high, open interest rose ≥ oi_min_pct over oi_lookback candles
    (new positions, not just short covering) and aggressive buyers dominate
    the breakout candle. Mirror for shorts."""
    out = [None] * ctx.n
    lb = p["oi_lookback"]
    for i in range(lb + 1, ctx.n):
        if not ctx.ready(i):
            continue
        oi_now, oi_then = ctx.oi[i], ctx.oi[i - lb]
        if oi_now is None or oi_then is None or oi_then <= 0:
            continue
        oi_chg = (oi_now / oi_then - 1) * 100
        if oi_chg < p["oi_min_pct"]:
            continue
        a, c = ctx.atr14[i], ctx.cl[i]
        sh, sl = ctx.swing_high[i - 1], ctx.swing_low[i - 1]
        tr = ctx.taker_ratio[i]
        if sh is not None and c > sh >= ctx.cl[i - 1] and tr >= p["taker_min"]:
            out[i] = _sig(ctx, i, "bos_oi", "LONG", c - p["stop_atr"] * a, p["rr"],
                          f"Break of structure di atas {_fmt(sh)}; open interest +{oi_chg:.1f}% (posisi baru masuk); "
                          f"pembeli agresif {tr:.0%}.", p["hold"])
        elif sl is not None and c < sl <= ctx.cl[i - 1] and tr <= 1 - p["taker_min"]:
            out[i] = _sig(ctx, i, "bos_oi", "SHORT", c + p["stop_atr"] * a, p["rr"],
                          f"Break of structure di bawah {_fmt(sl)}; open interest +{oi_chg:.1f}% (posisi baru masuk); "
                          f"penjual agresif {1 - tr:.0%}.", p["hold"])
    return out


# ------------------------------------------------------------------ registry
# defaults = the ONE parameter set that trades. grid = neighbours the Lab
# tests to make sure the defaults are not a lucky spike.
STRATEGIES: dict[str, dict] = {
    "ema_pullback": {"fn": ema_pullback, "needs_oi": False,
                     "defaults": {"stop_atr": 1.5, "rr": 1.5, "hold": 36},
                     "grid": [{"stop_atr": 1.2}, {"stop_atr": 1.8}, {"rr": 1.2}, {"rr": 2.0}]},
    "rsi_reversion": {"fn": rsi_reversion, "needs_oi": False,
                      "defaults": {"rsi_low": 30, "stop_atr": 1.2, "rr": 1.2, "hold": 24},
                      "grid": [{"rsi_low": 25}, {"rsi_low": 35}, {"stop_atr": 1.5}, {"rr": 1.5}]},
    "range_breakout": {"fn": range_breakout, "needs_oi": False,
                       "defaults": {"lookback": 20, "vol_mult": 1.5, "stop_atr": 1.0, "rr": 2.0, "hold": 36},
                       "grid": [{"lookback": 15}, {"lookback": 30}, {"vol_mult": 2.0}, {"rr": 1.5}]},
    "smc_sweep": {"fn": smc_sweep, "needs_oi": True,
                  "defaults": {"min_sweep_atr": 0.1, "stop_buf_atr": 0.2, "rr": 2.0, "use_oi": True,
                               "oi_lookback": 3, "funding_max": 0.0005, "hold": 36},
                  "grid": [{"min_sweep_atr": 0.05}, {"min_sweep_atr": 0.2}, {"rr": 1.5}, {"rr": 2.5},
                           {"use_oi": False}]},
    "fvg_retest": {"fn": fvg_retest, "needs_oi": False,
                   "defaults": {"max_age": 30, "min_gap_atr": 0.3, "stop_buf_atr": 0.2, "rr": 2.0, "hold": 36},
                   "grid": [{"max_age": 20}, {"max_age": 45}, {"min_gap_atr": 0.5}, {"rr": 1.5}]},
    "bos_oi": {"fn": bos_oi, "needs_oi": True,
               "defaults": {"oi_lookback": 6, "oi_min_pct": 0.5, "taker_min": 0.55, "stop_atr": 1.0,
                            "rr": 1.5, "hold": 36},
               "grid": [{"oi_min_pct": 0.3}, {"oi_min_pct": 1.0}, {"taker_min": 0.6}, {"rr": 2.0}]},
}


def params(name: str, override: dict | None = None) -> dict:
    p = dict(STRATEGIES[name]["defaults"])
    p.update(override or {})
    return p


def scan(name: str, ctx: Ctx, override: dict | None = None) -> list:
    p = params(name, override)
    key = (name, tuple(sorted(p.items())))
    if key not in ctx.cache:
        ctx.cache[key] = STRATEGIES[name]["fn"](ctx, p)
    return ctx.cache[key]
