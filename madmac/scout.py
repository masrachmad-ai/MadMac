"""SCOUT — watches the charts. One job: when a candle closes, check every
(strategy, coin) pair that passed the Lab and report setups. Scout never
sizes, approves or trades.

Market data ALWAYS comes from Binance's real (live) public market, also in
testnet mode, so signals are computed on real prices. If a coin's feed is
stale, Scout reports it and skips that coin."""
from __future__ import annotations

from .config import Config
from .features import Ctx
from .indicators import Candle
from .journal import Journal
from .strategies import STRATEGIES, scan

KLINES = 1000  # enough history for EMA200 to forget its seed


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
    def __init__(self, cfg: Config, market, journal: Journal):
        self.cfg, self.mkt, self.j = cfg, market, journal
        self.stale: dict[str, str] = {}

    def due(self, sym: str, now_ms: int) -> bool:
        last = self.j.get(f"scout_last:{sym}", 0)
        return now_ms >= last + self.cfg.tf_seconds * 1000 + 2000

    def scan(self, pairs: set[tuple[str, str]]) -> list:
        """pairs = (strategy, symbol) allowed by the Lab."""
        if not pairs:
            return []
        now_ms = int(self.mkt.server_time() * 1000)  # exchange clock → no timezone bugs
        out = []
        for sym in self.cfg.symbols:
            strats = [s for s, x in pairs if x == sym]
            if not strats or not self.due(sym, now_ms):
                continue
            try:
                candles = closed_only(self.mkt.klines(sym, self.cfg.timeframe, KLINES), now_ms)
                check_fresh(candles, now_ms, self.cfg.tf_seconds, self.cfg.max_feed_age_seconds)
            except StaleFeed as e:
                if sym not in self.stale:
                    self.j.log("scout", "stale_feed", f"{sym}: {e}")
                self.stale[sym] = str(e)
                continue
            self.stale.pop(sym, None)
            self.j.set(f"scout_last:{sym}", candles[-1].close_time)
            oi = funding = None
            if any(STRATEGIES[s]["needs_oi"] for s in strats):
                tf_ms = self.cfg.tf_seconds * 1000  # same one-period publication delay as the Lab
                oi = [(t + tf_ms, v) for t, v in self.mkt.open_interest_hist(sym, self.cfg.timeframe, 200)]
                funding = [(candles[-1].close_time, self.mkt.funding_now(sym)[1])]  # current rate
            ctx = Ctx(candles, sym, oi=oi, funding=funding)
            for name in strats:
                sig = scan(name, ctx)[-1]
                if sig:
                    self.j.log("scout", "signal", f"{sym} {name} {sig.side} @ {sig.entry:,.6g} — {sig.reason}",
                               symbol=sym, stop=sig.stop, target=sig.target, candle=sig.candle_time)
                    out.append(sig)
        return out
