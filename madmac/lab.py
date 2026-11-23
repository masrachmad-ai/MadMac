"""LAB — backtests every (strategy, coin) pair on REAL Binance history before
it goes near money. One job: produce honest numbers, after fees and
slippage, and mark a pair PASSED only if it survives every test below.

Data: Binance's official public archive data.binance.vision — klines (with
taker buy volume), funding rate, and the "metrics" files (open interest) —
each file verified against its published SHA-256 checksum. Every result
records the exact time range and sources.

Simulation is deliberately pessimistic:
  * signal decided at candle close; trading starts on the NEXT candle
  * maker entry fills only if price trades THROUGH the limit (touch ≠ fill)
  * on the fill candle only the stop is checked; stop beats target on ties
  * take-profit is a maker limit that also needs price to trade through
  * stop / time exits pay taker fee + slippage
  * open interest is used one period LATE (publication delay)

A pair PASSES only if ALL hold (numbers from config.yaml → lab):
  1. enough trades in total and out-of-sample
  2. out-of-sample profit factor ≥ min_profit_factor, in-sample not losing
  3. walk-forward: profitable in ≥ min_positive_folds_pct of time folds
  4. parameter sensitivity: neighbouring settings are not losing (median PF ≥ 1)
  5. beats random entries with the same risk geometry, after a
     Benjamini-Hochberg false-discovery correction over ALL pairs tested
  6. Monte Carlo: 95th-percentile drawdown ≤ max_drawdown_pct
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
import statistics
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from .config import DATA_DIR, Config
from .features import WARMUP, Ctx
from .indicators import Candle
from .shield import lab_key
from .strategies import STRATEGIES, Signal, scan

ROOT_URL = "https://data.binance.vision/data/futures/um"
KLINES_URL = ROOT_URL + "/monthly/klines/{s}/{tf}/{s}-{tf}-{ym}.zip"
FUNDING_URL = ROOT_URL + "/monthly/fundingRate/{s}/{s}-fundingRate-{ym}.zip"
METRICS_URL = ROOT_URL + "/daily/metrics/{s}/{s}-metrics-{d}.zip"


# ====================================================================== data
def last_full_months(n: int, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y}-{m:02d}")
    return sorted(out)


def _fetch(url: str, cache: Path) -> bytes | None:
    """Download + checksum-verify one archive file (cached). None if absent."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / url.rsplit("/", 1)[1]
    if path.exists():
        return path.read_bytes()
    r = requests.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    c = requests.get(url + ".CHECKSUM", timeout=30)
    c.raise_for_status()
    if c.text.split()[0].strip().lower() != hashlib.sha256(r.content).hexdigest():
        raise ValueError(f"Checksum {path.name} tidak cocok — data ditolak")
    path.write_bytes(r.content)
    return r.content


def _rows(blob: bytes) -> tuple[list[str] | None, list[list[str]]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        with z.open(z.namelist()[0]) as f:
            rows = [r for r in csv.reader(io.TextIOWrapper(f)) if r]
    if rows and not rows[0][0].replace(".", "").isdigit():
        return rows[0], rows[1:]
    return None, rows


def load_klines(sym: str, tf: str, months: list[str], cache: Path) -> tuple[list[Candle], list[str]]:
    out, src = [], []
    for ym in months:
        url = KLINES_URL.format(s=sym, tf=tf, ym=ym)
        blob = _fetch(url, cache / sym)
        if blob is None:
            continue
        _, rows = _rows(blob)
        for r in rows:
            out.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                              float(r[5]), int(r[6]), float(r[9])))
        src.append(url)
    out.sort(key=lambda c: c.open_time)
    return out, src


def load_funding(sym: str, months: list[str], cache: Path) -> list[tuple[int, float]]:
    pts = []
    for ym in months:
        blob = _fetch(FUNDING_URL.format(s=sym, ym=ym), cache / sym)
        if blob is None:
            continue
        head, rows = _rows(blob)
        ti = head.index("calc_time") if head and "calc_time" in head else 0
        vi = head.index("last_funding_rate") if head and "last_funding_rate" in head else 2
        pts += [(int(r[ti]), float(r[vi])) for r in rows]
    return pts


def load_open_interest(sym: str, months: list[str], cache: Path, delay_ms: int) -> list[tuple[int, float]]:
    first = date.fromisoformat(months[0] + "-01")
    y, m = map(int, months[-1].split("-"))
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    days = [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]

    def one(d: str):
        try:
            blob = _fetch(METRICS_URL.format(s=sym, d=d), cache / sym / "metrics")
        except (requests.RequestException, ValueError):
            return []
        if blob is None:
            return []
        head, rows = _rows(blob)
        if not head or "sum_open_interest_value" not in head:
            return []
        ti, vi = head.index("create_time"), head.index("sum_open_interest_value")
        pts = []
        for r in rows:
            try:
                ts = int(datetime.strptime(r[ti], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
                pts.append((ts + delay_ms, float(r[vi])))
            except (ValueError, IndexError):
                continue
        return pts

    with ThreadPoolExecutor(8) as ex:
        return [p for chunk in ex.map(one, days) for p in chunk]


# ====================================================================== engine
@dataclass
class Trade:
    i: int          # signal candle index
    side: str
    r: float        # net result in R (1R = planned loss incl. costs)
    ret: float      # fraction of equity gained/lost


def simulate_one(ctx: Ctx, i: int, s: Signal, cfg: Config) -> tuple[int, float] | None:
    """Return (exit_index, net_R) or None if not filled / skipped."""
    long = s.side == "LONG"
    fee_in = cfg.entry_fee_pct / 100
    fee_tk, fee_mk, slip = cfg.taker_fee_pct / 100, cfg.maker_fee_pct / 100, cfg.slippage_pct / 100
    n = ctx.n
    # ---- entry
    if cfg.entry_order == "maker":
        E, fill = s.entry, None
        if i + 1 >= n or (long and ctx.o[i + 1] < E) or (not long and ctx.o[i + 1] > E):
            return None  # price already through the limit → post-only order is rejected live
        for k in range(i + 1, min(n, i + 1 + cfg.entry_timeout_candles)):
            if (long and ctx.l[k] < E) or (not long and ctx.h[k] > E):
                fill = k
                break
        if fill is None:
            return None
    else:
        fill = i + 1
        if fill >= n:
            return None
        E = ctx.o[fill] * (1 + slip if long else 1 - slip)
    S, T = s.stop, s.target
    if (long and E <= S) or (not long and E >= S):
        return None
    stop_px = S * (1 - slip if long else 1 + slip)
    risk_unit = abs(E - stop_px) + E * fee_in + stop_px * fee_tk
    if (E * fee_in + E * fee_tk) / risk_unit * 100 > cfg.max_fee_share_of_risk_pct:
        return None

    def net(X: float, fee_out: float) -> float:
        gross = (X - E) if long else (E - X)
        return (gross - E * fee_in - X * fee_out) / risk_unit

    # ---- exit
    last = min(n - 1, fill + s.max_hold)
    for k in range(fill, last + 1):
        hit_stop = ctx.l[k] <= S if long else ctx.h[k] >= S
        if hit_stop:
            return k, net(stop_px, fee_tk)
        if k == fill and cfg.entry_order == "maker":
            continue  # fill candle: we don't know if target came after the fill
        if (long and ctx.h[k] > T) or (not long and ctx.l[k] < T):
            return k, net(T, fee_mk)
    if last == n - 1 and last < fill + s.max_hold:
        return None  # still open at end of data → not counted
    X = ctx.cl[last] * (1 - slip if long else 1 + slip)
    return last, net(X, fee_tk)


def size_scale(s: Signal, cfg: Config) -> float:
    """Shrink factor when the leverage cap binds (same rule as Shield)."""
    stop_frac = abs(s.entry - s.stop) / s.entry
    if stop_frac <= 0:
        return 0.0
    exposure = (cfg.risk_per_trade_pct / 100) / stop_frac       # notional / equity
    cap = 0.9 * cfg.max_leverage / cfg.max_open_positions
    return min(1.0, cap / exposure)


def run_signals(ctx: Ctx, sigs: list, cfg: Config, lo: int = 0, hi: int | None = None) -> list[Trade]:
    hi = ctx.n if hi is None else hi
    out: list[Trade] = []
    busy_until = -1
    day, day_ret, halted = None, 0.0, False
    for i in range(max(lo, WARMUP), hi):
        s = sigs[i]
        if s is None or i <= busy_until:
            continue
        d = ctx.t[i] // 86_400_000
        if d != day:
            day, day_ret, halted = d, 0.0, False
        if halted:
            continue
        res = simulate_one(ctx, i, s, cfg)
        if res is None:
            continue
        k, r = res
        ret = r * cfg.risk_per_trade_pct / 100 * size_scale(s, cfg)
        out.append(Trade(i, s.side, r, ret))
        busy_until = k
        day_ret += ret
        if day_ret * 100 <= -cfg.daily_loss_limit_pct:
            halted = True
    return out


# ====================================================================== stats
@dataclass
class Stats:
    trades: int = 0
    wins: int = 0
    profit_factor: float | None = None
    expectancy_r: float = 0.0
    return_pct: float = 0.0
    max_dd_pct: float = 0.0

    @classmethod
    def of(cls, tr: list[Trade]) -> "Stats":
        st = cls(trades=len(tr), wins=sum(t.r > 0 for t in tr))
        gw = sum(t.ret for t in tr if t.ret > 0)
        gl = -sum(t.ret for t in tr if t.ret < 0)
        st.profit_factor = round(gw / gl, 3) if gl > 0 else (None if gw == 0 else 99.0)
        st.expectancy_r = round(statistics.fmean([t.r for t in tr]), 4) if tr else 0.0
        eq, peak, dd = 1.0, 1.0, 0.0
        for t in tr:
            eq *= 1 + t.ret
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        st.return_pct, st.max_dd_pct = round((eq - 1) * 100, 2), round(dd * 100, 2)
        return st

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    def pf(self) -> float:
        return self.profit_factor or 0.0

    def as_dict(self) -> dict:
        return {**self.__dict__, "win_rate": round(self.win_rate, 1)}


def folds_positive(tr: list[Trade], n: int, k: int) -> tuple[int, int]:
    """(# folds with profit, # folds with ≥5 trades)."""
    size = max(1, (n - WARMUP) // k)
    pos = counted = 0
    for f in range(k):
        lo, hi = WARMUP + f * size, WARMUP + (f + 1) * size
        ft = [t for t in tr if lo <= t.i < hi]
        if len(ft) < 5:
            continue
        counted += 1
        pos += sum(t.ret for t in ft) > 0
    return pos, counted


def random_baseline(ctx: Ctx, sigs: list, cfg: Config, draws: int, seed: int) -> list[float]:
    """Net R of random entries (random time, random side) using the SAME
    stop/target geometry (in ATR units) and hold as the strategy's signals."""
    geo = []
    for i, s in enumerate(sigs):
        if s and ctx.atr14[i]:
            k = abs(s.entry - s.stop) / ctx.atr14[i]
            geo.append((k, abs(s.target - s.entry) / abs(s.entry - s.stop), s.max_hold))
    if not geo:
        return []
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        i = rng.randrange(WARMUP, ctx.n - 2)
        a = ctx.atr14[i]
        if not a:
            continue
        k, rr, hold = rng.choice(geo)
        side = rng.choice(("LONG", "SHORT"))
        e = ctx.cl[i]
        stop = e - k * a if side == "LONG" else e + k * a
        tgt = e + rr * k * a if side == "LONG" else e - rr * k * a
        res = simulate_one(ctx, i, Signal("random", side, e, stop, tgt, "", ctx.t[i], ctx.symbol, hold), cfg)
        if res:
            out.append(res[1])
    return out


def edge_p_value(strat_r: list[float], rand_r: list[float]) -> float:
    """One-sided Welch test: is the strategy's mean R above random's?"""
    if len(strat_r) < 10 or len(rand_r) < 30:
        return 1.0
    m1, m2 = statistics.fmean(strat_r), statistics.fmean(rand_r)
    v1, v2 = statistics.variance(strat_r), statistics.variance(rand_r)
    se = math.sqrt(v1 / len(strat_r) + v2 / len(rand_r))
    if se == 0:
        return 0.0 if m1 > m2 else 1.0
    return 0.5 * math.erfc(((m1 - m2) / se) / math.sqrt(2))


def benjamini_hochberg(pvals: dict[str, float], q: float) -> set[str]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    cut = 0
    for rank, (_, p) in enumerate(items, 1):
        if p <= q * rank / m:
            cut = rank
    return {k for k, _ in items[:cut]}


def monte_carlo_dd(tr: list[Trade], runs: int, seed: int) -> float:
    """95th-percentile max drawdown (%) over random re-orderings of trades."""
    if len(tr) < 2:
        return 0.0
    rets = [t.ret for t in tr]
    rng = random.Random(seed)
    dds = []
    for _ in range(runs):
        rng.shuffle(rets)
        eq = peak = 1.0
        dd = 0.0
        for r in rets:
            eq *= 1 + r
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        dds.append(dd)
    dds.sort()
    return round(dds[int(0.95 * (len(dds) - 1))] * 100, 2)


# ====================================================================== judge
@dataclass
class PairResult:
    strategy: str
    symbol: str
    data: dict = field(default_factory=dict)
    why_not: list[str] = field(default_factory=list)
    p_value: float = 1.0


def evaluate_pair(ctx: Ctx, name: str, cfg: Config, seed: int = 7) -> PairResult:
    L = cfg.lab
    res = PairResult(name, ctx.symbol)
    if STRATEGIES[name]["needs_oi"] and not ctx.has_oi:
        res.why_not.append("data open interest tidak tersedia")
        res.data = {"note": "tidak diuji"}
        return res
    sigs = scan(name, ctx)
    split = WARMUP + int((ctx.n - WARMUP) * (1 - L.out_of_sample_pct / 100))
    full = run_signals(ctx, sigs, cfg)
    ins = [t for t in full if t.i < split]
    oos = [t for t in full if t.i >= split]
    s_in, s_oos, s_full = Stats.of(ins), Stats.of(oos), Stats.of(full)

    if s_full.trades < L.min_trades:
        res.why_not.append(f"terlalu sedikit trade ({s_full.trades} < {L.min_trades}) — bisa kebetulan")
    if s_oos.trades < 20:
        res.why_not.append(f"terlalu sedikit trade out-of-sample ({s_oos.trades} < 20)")
    if s_oos.pf() < L.min_profit_factor:
        res.why_not.append(f"profit factor out-of-sample {s_oos.pf():.2f} < {L.min_profit_factor}")
    if s_in.pf() < 1.0:
        res.why_not.append(f"rugi di data in-sample (PF {s_in.pf():.2f})")
    if L.min_win_rate_pct and s_oos.win_rate < L.min_win_rate_pct:
        res.why_not.append(f"win rate {s_oos.win_rate:.0f}% < {L.min_win_rate_pct}%")

    pos, counted = folds_positive(full, ctx.n, L.folds)
    if counted == 0 or pos / counted * 100 < L.min_positive_folds_pct:
        res.why_not.append(f"walk-forward: hanya {pos}/{counted} periode untung (min {L.min_positive_folds_pct:.0f}%)")

    neigh = []
    for g in STRATEGIES[name]["grid"]:
        tr = [t for t in run_signals(ctx, scan(name, ctx, g), cfg) if t.i < split]
        neigh.append(Stats.of(tr).pf())
    med = statistics.median(neigh) if neigh else 0.0
    if med < 1.0:
        res.why_not.append(f"sensitif parameter: median PF pengaturan tetangga {med:.2f} < 1")

    rand = random_baseline(ctx, sigs, cfg, L.random_baseline_draws, seed)
    res.p_value = edge_p_value([t.r for t in full], rand)

    mc = monte_carlo_dd(oos, L.monte_carlo_runs, seed)
    if mc > L.max_drawdown_pct:
        res.why_not.append(f"Monte Carlo drawdown 95% = {mc:.1f}% > {L.max_drawdown_pct}%")

    res.data = {
        "in_sample": s_in.as_dict(), "out_of_sample": s_oos.as_dict(), "full": s_full.as_dict(),
        "folds_positive": f"{pos}/{counted}", "neighbour_pf": [round(x, 2) for x in neigh],
        "random_mean_r": round(statistics.fmean(rand), 4) if rand else None,
        "edge_p_value": round(res.p_value, 5), "mc_dd95_pct": mc,
        "oos_from": _iso(ctx.t[split]) if split < ctx.n else None,
    }
    return res


def finalize(results: list[PairResult], cfg: Config, meta: dict) -> dict:
    tested = {lab_key(r.strategy, r.symbol): r.p_value for r in results if r.data.get("note") != "tidak diuji"}
    significant = benjamini_hochberg(tested, cfg.lab.false_discovery_rate) if tested else set()
    pairs = {}
    now = int(time.time())
    for r in results:
        k = lab_key(r.strategy, r.symbol)
        why = list(r.why_not)
        if k in tested and k not in significant:
            why.append(f"tidak terbukti lebih baik dari entry acak setelah koreksi {len(tested)} uji "
                       f"(p={r.p_value:.3f})")
        pairs[k] = {"strategy": r.strategy, "symbol": r.symbol, "timeframe": cfg.timeframe,
                    "passed": not why, "why_not": why, "tested_at": now,
                    **r.data, **meta.get(r.symbol, {})}
    return {"generated_at": _iso(now * 1000), "pairs_tested": len(tested),
            "assumptions": {"entry_order": cfg.entry_order, "maker_fee_pct": cfg.maker_fee_pct,
                            "taker_fee_pct": cfg.taker_fee_pct, "slippage_pct": cfg.slippage_pct,
                            "risk_per_trade_pct": cfg.risk_per_trade_pct, "max_leverage": cfg.max_leverage,
                            "note": "fee & slippage adalah ESTIMASI dari config.yaml"},
            "pairs": pairs}


def run(cfg: Config, months: list[str] | None = None, out_dir: Path | None = None,
        ctxs: dict[str, Ctx] | None = None, log=print) -> dict:
    out_dir = out_dir or DATA_DIR
    months = months or last_full_months(cfg.lab.months_of_data)
    tf_ms = cfg.tf_seconds * 1000
    meta: dict[str, dict] = {}
    if ctxs is None:
        ctxs = {}
        need_oi = any(STRATEGIES[s]["needs_oi"] for s in cfg.strategies)
        for sym in cfg.symbols:
            log(f"[{sym}] unduh data {months[0]} … {months[-1]}")
            candles, src = load_klines(sym, cfg.timeframe, months, out_dir / "history")
            if len(candles) < WARMUP + 500:
                log(f"[{sym}] data tidak cukup ({len(candles)} candle) — dilewati")
                continue
            oi = load_open_interest(sym, months, out_dir / "history", tf_ms) if need_oi else None
            funding = load_funding(sym, months, out_dir / "history")
            ctxs[sym] = Ctx(candles, sym, oi=oi, funding=funding)
            meta[sym] = {"data_from": _iso(candles[0].open_time), "data_to": _iso(candles[-1].close_time),
                         "candles": len(candles), "sources": src,
                         "oi_points": len(oi or []), "funding_points": len(funding)}
    results = []
    for sym, ctx in ctxs.items():
        for name in cfg.strategies:
            log(f"[{sym}] uji {name}")
            results.append(evaluate_pair(ctx, name, cfg))
    out = finalize(results, cfg, meta)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "lab_results.json").write_text(json.dumps(out, indent=2))
    (out_dir / "lab_report.md").write_text(report(out))
    return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def report(res: dict) -> str:
    pairs = res["pairs"]
    passed = [v for v in pairs.values() if v["passed"]]
    lines = ["# Laporan Lab", "",
             f"Dibuat {res['generated_at']}. {res['pairs_tested']} pasangan strategi×koin diuji.",
             "Semua angka dari data asli Binance, SETELAH fee & slippage (estimasi di config.yaml).",
             "Hasil masa lalu BUKAN jaminan hasil masa depan.", "",
             f"## Lulus: {len(passed)}", ""]
    if not passed:
        lines += ["Tidak ada yang lulus. Bot hanya memantau — itu hasil yang jujur, bukan error.", ""]
    head = ["| Strategi | Koin | Trade OOS | PF OOS | Expectancy (R) | Walk-forward | p vs acak | DD MC95 | Status |",
            "|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for v in sorted(pairs.values(), key=lambda v: (not v["passed"], v["symbol"], v["strategy"])):
        o = v.get("out_of_sample")
        if not o:
            rows.append(f"| {v['strategy']} | {v['symbol']} | – | – | – | – | – | – | ❌ {'; '.join(v['why_not'])} |")
            continue
        rows.append(f"| {v['strategy']} | {v['symbol']} | {o['trades']} | {o['profit_factor']} | "
                    f"{v['full']['expectancy_r']} | {v['folds_positive']} | {v['edge_p_value']} | "
                    f"{v['mc_dd95_pct']}% | {'✅' if v['passed'] else '❌ ' + '; '.join(v['why_not'])} |")
    lines += ["## Semua pasangan", ""] + head + rows + ["",
              "PF = profit factor (> 1 berarti untung). R = kelipatan risiko per trade. "
              "p vs acak kecil = sinyal lebih baik dari entry acak.", ""]
    return "\n".join(lines)
