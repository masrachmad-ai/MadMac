"""SCOUT — watches the chart. One job: on every closed candle, check the
enabled rules and report setups. Scout never sizes, approves or trades.

If the price feed is stale (last closed candle older than it should be),
Scout reports the problem and returns no signals."""
from __future__ import annotations

from .binance import Binance
from .config import Config
from .indicators import Candle
from .journal import Journal
from .strategies import STRATEGIES, Signal


class StaleFeed(Exception):
    pass


def closed_only(candles: list[Candle], now_ms: int) -> list[Candle]:
    return [c for c in candles if c.close_time < now_ms]


def check_fresh(candles: list[Candle], now_ms: int, tf_seconds: int, max_age_s: int) -> None:
    if not candles:
        raise StaleFeed("tidak ada data candle")
    lag_s = (now_ms - candles[-1].close_time) / 1000
    if lag_s > tf_seconds + max_age_s:
        raise StaleFeed(f"candle terakhir sudah {lag_s:.0f} detik lalu")


class Scout:
    def __init__(self, cfg: Config, ex: Binance, journal: Journal):
        self.cfg, self.ex, self.j = cfg, ex, journal

    def scan(self) -> list[Signal]:
        now_ms = int(self.ex.server_time() * 1000)  # exchange clock, not ours → no timezone bugs
        candles = closed_only(self.ex.klines(self.cfg.symbol, self.cfg.timeframe, 300), now_ms)
        check_fresh(candles, now_ms, self.cfg.tf_seconds, self.cfg.max_feed_age_seconds)
        last_seen = self.j.get("scout_last_candle", 0)
        if candles[-1].close_time <= last_seen:
            return []
        self.j.set("scout_last_candle", candles[-1].close_time)
        out = []
        for name in self.cfg.strategies:
            sig = STRATEGIES[name](candles)
            if sig:
                self.j.log("scout", "signal", f"{sig.strategy} {sig.side} @ {sig.entry:,.1f} — {sig.reason}",
                           stop=sig.stop, target=sig.target, candle=sig.candle_time)
                out.append(sig)
        return out
