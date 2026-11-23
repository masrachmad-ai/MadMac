"""LAB — backtests every rule on REAL Binance history before it goes near
money. One job: produce honest numbers, after fees and slippage, and mark a
strategy PASSED only if it holds up on data it was not tuned on.

Data source: Binance's official public archive, data.binance.vision
(USDⓈ-M futures monthly klines), verified against the published SHA-256
checksum. Every result records the exact files and time range used.

Simulation is deliberately pessimistic:
  * entry at the NEXT candle's open + slippage (no trading on the signal candle)
  * stop and target both inside one candle → counted as a STOP
  * taker fee on entry and exit, slippage on both sides
  * same sizing, leverage cap, fee guard and daily loss limit as Shield
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import DATA_DIR, Config
from .indicators import Candle
from .strategies import STRATEGIES

ARCHIVE = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/{tf}/{s}-{tf}-{ym}.zip"
WINDOW = 300  # same number of candles Scout sees live


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


def download_month(symbol: str, tf: str, ym: str, cache: Path) -> tuple[list[Candle], str]:
    url = ARCHIVE.format(s=symbol, tf=tf, ym=ym)
    cache.mkdir(parents=True, exist_ok=True)
    zpath = cache / url.rsplit("/", 1)[1]
    if not zpath.exists():
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        c = requests.get(url + ".CHECKSUM", timeout=30)
        c.raise_for_status()
        want = c.text.split()[0].strip().lower()
        got = hashlib.sha256(r.content).hexdigest()
        if want != got:
            raise ValueError(f"Checksum {zpath.name} tidak cocok — data ditolak")
        zpath.write_bytes(r.content)
    out = []
    with zipfile.ZipFile(zpath) as z:
        with z.open(z.namelist()[0]) as f:
            for row in csv.reader(io.TextIOWrapper(f)):
                if not row or not row[0].isdigit():
                    continue  # header line
                out.append(Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                                  float(row[4]), float(row[5]), int(row[6])))
    return out, url


@dataclass
class Stats:
    trades: int = 0
    wins: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    fees: float = 0.0
    start_equity: float = 1000.0
    end_equity: float = 1000.0
    max_dd_pct: float = 0.0
    skipped_fee_guard: int = 0
    skipped_too_small: int = 0

    @property
    def profit_factor(self) -> float:
        return self.gross_win / self.gross_loss if self.gross_loss else (math.inf if self.gross_win else 0.0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def return_pct(self) -> float:
        return (self.end_equity / self.start_equity - 1) * 100

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(profit_factor=round(self.profit_factor, 3) if math.isfinite(self.profit_factor) else None,
                 win_rate=round(self.win_rate, 1), return_pct=round(self.return_pct, 2))
        return d


def simulate(candles: list[Candle], strategy: str, cfg: Config, equity: float = 1000.0,
             min_notional: float = 100.0) -> Stats:
    fn = STRATEGIES[strategy]
    st = Stats(start_equity=equity, end_equity=equity)
    fee = cfg.taker_fee_pct / 100
    slip = cfg.slippage_pct / 100
    peak = equity
    i = WINDOW
    day, day_start_eq, day_halted = None, equity, False
    while i < len(candles) - 1:
        d = candles[i].close_time // 86_400_000
        if d != day:
            day, day_start_eq, day_halted = d, equity, False
        if day_halted:
            i += 1
            continue
        sig = fn(candles[i - WINDOW + 1:i + 1])
        if not sig:
            i += 1
            continue
        nxt = candles[i + 1]
        long = sig.side == "LONG"
        entry = nxt.open * (1 + slip if long else 1 - slip)
        if (long and entry <= sig.stop) or (not long and entry >= sig.stop):
            i += 1
            continue
        stop_dist = abs(entry - sig.stop)
        per_unit = stop_dist + entry * 2 * (fee + slip)
        risk = equity * cfg.risk_per_trade_pct / 100
        qty = risk / per_unit
        qty = min(qty, equity * 0.9 * cfg.max_leverage / entry)
        notional = qty * entry
        if notional < min_notional:
            st.skipped_too_small += 1
            i += 1
            continue
        fees_rt = notional * 2 * fee
        if fees_rt / (qty * per_unit) * 100 > cfg.max_fee_share_of_risk_pct:
            st.skipped_fee_guard += 1
            i += 1
            continue
        # walk forward from the entry candle until stop or target
        exit_px, j = None, i + 1
        while j < len(candles):
            c = candles[j]
            hit_stop = c.low <= sig.stop if long else c.high >= sig.stop
            hit_tgt = c.high >= sig.target if long else c.low <= sig.target
            if hit_stop:  # pessimistic: stop wins ties
                exit_px = sig.stop * (1 - slip if long else 1 + slip)
                break
            if hit_tgt:
                exit_px = sig.target * (1 - slip if long else 1 + slip)
                break
            j += 1
        if exit_px is None:
            break  # trade still open at end of data → not counted
        gross = (exit_px - entry) * qty if long else (entry - exit_px) * qty
        cost = entry * qty * fee + exit_px * qty * fee
        net = gross - cost
        equity += net
        st.trades += 1
        st.fees += cost
        if net > 0:
            st.wins += 1
            st.gross_win += net
        else:
            st.gross_loss += -net
        peak = max(peak, equity)
        st.max_dd_pct = max(st.max_dd_pct, (peak - equity) / peak * 100)
        if (equity - day_start_eq) / day_start_eq * 100 <= -cfg.daily_loss_limit_pct:
            day_halted = True
        if equity <= 0:
            break
        i = j + 1  # one position at a time
    st.end_equity = equity
    return st


def judge(oos: Stats, ins: Stats, cfg: Config) -> tuple[bool, list[str]]:
    r = cfg.lab
    why = []
    if ins.trades + oos.trades < r.min_trades:
        why.append(f"terlalu sedikit trade ({ins.trades + oos.trades} < {r.min_trades}) — bisa kebetulan")
    if oos.trades < 20:
        why.append(f"terlalu sedikit trade out-of-sample ({oos.trades} < 20)")
    if oos.profit_factor < r.min_profit_factor:
        why.append(f"profit factor out-of-sample {oos.profit_factor:.2f} < {r.min_profit_factor}")
    if ins.profit_factor < 1.0:
        why.append(f"rugi juga di data in-sample (PF {ins.profit_factor:.2f})")
    if oos.max_dd_pct > r.max_drawdown_pct:
        why.append(f"drawdown {oos.max_dd_pct:.1f}% > {r.max_drawdown_pct}%")
    if r.min_win_rate_pct and oos.win_rate < r.min_win_rate_pct:
        why.append(f"win rate {oos.win_rate:.0f}% < {r.min_win_rate_pct}%")
    return (not why), why


def run(cfg: Config, months: list[str] | None = None, candles: list[Candle] | None = None,
        sources: list[str] | None = None, out_dir: Path | None = None) -> dict:
    out_dir = out_dir or DATA_DIR
    if candles is None:
        months = months or last_full_months(cfg.lab.months_of_data)
        candles, sources = [], []
        for ym in months:
            c, url = download_month(cfg.symbol, cfg.timeframe, ym, out_dir / "history")
            candles += c
            sources.append(url)
    candles.sort(key=lambda c: c.open_time)
    split = int(len(candles) * (1 - cfg.lab.out_of_sample_pct / 100))
    results = {}
    for name in cfg.strategies:
        ins = simulate(candles[:split], name, cfg)
        oos = simulate(candles[split - WINDOW:], name, cfg)
        passed, why = judge(oos, ins, cfg)
        results[name] = {
            "passed": passed, "why_not": why, "symbol": cfg.symbol, "timeframe": cfg.timeframe,
            "tested_at": int(time.time()),
            "data_from": _iso(candles[0].open_time), "data_to": _iso(candles[-1].close_time),
            "oos_from": _iso(candles[split].open_time), "candles": len(candles),
            "sources": sources or ["(disediakan pemanggil)"],
            "assumptions": {"taker_fee_pct": cfg.taker_fee_pct, "slippage_pct": cfg.slippage_pct,
                            "risk_per_trade_pct": cfg.risk_per_trade_pct, "max_leverage": cfg.max_leverage,
                            "fee_and_slippage_are": "ESTIMASI dari config.yaml"},
            "in_sample": ins.as_dict(), "out_of_sample": oos.as_dict(),
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "lab_results.json").write_text(json.dumps(results, indent=2))
    (out_dir / "lab_report.md").write_text(report(results))
    return results


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def report(results: dict) -> str:
    lines = ["# Laporan Lab", "",
             "Semua angka dihitung dari data asli Binance SETELAH fee & slippage (estimasi di config.yaml).",
             "Hasil masa lalu BUKAN jaminan hasil masa depan.", ""]
    for name, r in results.items():
        o, i = r["out_of_sample"], r["in_sample"]
        lines += [f"## {name}: {'✅ LULUS' if r['passed'] else '❌ TIDAK LULUS'}",
                  f"Data: {r['data_from']} → {r['data_to']} ({r['candles']} candle). Out-of-sample mulai {r['oos_from']}.",
                  "",
                  "| | In-sample | Out-of-sample |", "|---|---|---|",
                  f"| Jumlah trade | {i['trades']} | {o['trades']} |",
                  f"| Win rate | {i['win_rate']}% | {o['win_rate']}% |",
                  f"| Profit factor | {i['profit_factor']} | {o['profit_factor']} |",
                  f"| Return | {i['return_pct']}% | {o['return_pct']}% |",
                  f"| Max drawdown | {i['max_dd_pct']:.1f}% | {o['max_dd_pct']:.1f}% |",
                  f"| Total fee (dari modal awal $1.000) | ${i['fees']:.2f} | ${o['fees']:.2f} |",
                  f"| Ditolak penjaga fee | {i['skipped_fee_guard']} | {o['skipped_fee_guard']} |", ""]
        if r["why_not"]:
            lines += ["Alasan tidak lulus: " + "; ".join(r["why_not"]), ""]
        lines += ["Sumber: " + ", ".join(r["sources"]), ""]
    return "\n".join(lines)
