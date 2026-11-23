"""SHIELD — the risk manager. One job: say YES or NO to a trade, and size it.

Shield never sends orders. It only returns a Decision. Pilot is not allowed
to send an entry order without an approved Decision. Shield also owns the
emergency brake (halt) that stops all new entries in seconds.

Every number Shield uses comes from the exchange (equity, price, filters)
or the Journal (today's results). If any input is missing or stale, the
answer is NO.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR, Config
from .journal import Journal, utc_day_start
from .strategies import Signal

# Approximate maintenance margin for BTCUSDT's lowest bracket. ESTIMATE —
# used only to keep the stop well away from liquidation.
APPROX_MAINT_MARGIN_PCT = 0.5
LAB_RESULT_MAX_AGE_DAYS = 30


@dataclass(frozen=True)
class SymbolRules:
    step_size: float      # qty increment
    min_qty: float
    min_notional: float   # USDT
    tick_size: float      # price increment


@dataclass(frozen=True)
class MarketSnapshot:
    equity_usdt: float           # margin balance from exchange
    price: float                 # latest trade/mark price
    price_ts: float              # unix seconds when that price was observed
    open_position_qty: float     # signed, from exchange (0 = flat)
    rules: SymbolRules


@dataclass
class Decision:
    approved: bool
    reasons: list[str] = field(default_factory=list)   # why NO (plain language)
    qty: float = 0.0
    leverage: int = 0
    risk_usdt: float = 0.0
    notional_usdt: float = 0.0
    est_fees_usdt: float = 0.0

    def text(self) -> str:
        if self.approved:
            return (f"Shield ✅ qty {self.qty} BTC · leverage {self.leverage}x · "
                    f"risiko ${self.risk_usdt:.2f} · nilai posisi ${self.notional_usdt:,.0f} · "
                    f"fee est. ${self.est_fees_usdt:.2f}")
        return "Shield ⛔ " + "; ".join(self.reasons)


def floor_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    n = math.floor(x / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals)


def load_lab_results(path: Path | None = None) -> dict:
    p = path or DATA_DIR / "lab_results.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


class Shield:
    def __init__(self, cfg: Config, journal: Journal, lab_results: dict | None = None):
        self.cfg = cfg
        self.j = journal
        self.lab = lab_results if lab_results is not None else load_lab_results()

    # ------------------------------------------------------------ brake
    def halt(self, why: str, until_next_day: bool = False) -> None:
        self.j.set("halt", {"why": why, "ts": int(time.time()),
                            "until_day": utc_day_start() + 86400 if until_next_day else None})
        self.j.log("shield", "halt", why)

    def resume(self, who: str) -> None:
        self.j.set("halt", None)
        self.j.log("shield", "resume", f"Dilanjutkan oleh {who}")

    def halted(self, now: float | None = None) -> str | None:
        h = self.j.get("halt")
        if not h:
            return None
        if h.get("until_day") and (now or time.time()) >= h["until_day"]:
            self.j.set("halt", None)
            self.j.log("shield", "resume", "Hari baru (00:00 UTC): rem harian dilepas otomatis.")
            return None
        return h["why"]

    # ------------------------------------------------------------ daily guard
    def update_day(self, equity: float, now: float | None = None) -> None:
        """Record start-of-day equity and trip the daily brakes if needed."""
        day = utc_day_start(now)
        rec = self.j.get("day_start")
        if not rec or rec["day"] != day:
            self.j.set("day_start", {"day": day, "equity": equity})
            rec = {"day": day, "equity": equity}
        start = rec["equity"]
        if start > 0:
            dd = (equity - start) / start * 100
            if dd <= -self.cfg.daily_loss_limit_pct and not self.halted(now):
                self.halt(f"Rugi hari ini {dd:.2f}% (batas −{self.cfg.daily_loss_limit_pct}%). "
                          "Tidak ada entry baru sampai 07:00 WIB.", until_next_day=True)
        streak = self.j.consecutive_losses()
        if streak >= self.cfg.max_consecutive_losses and not self.halted(now):
            self.halt(f"{streak} kali rugi beruntun (batas {self.cfg.max_consecutive_losses}). "
                      "Bot berhenti sampai Anda ketik /resume setelah meninjau.")

    # ------------------------------------------------------------ main check
    def check(self, sig: Signal, m: MarketSnapshot, now: float | None = None) -> Decision:
        now = now or time.time()
        cfg = self.cfg
        no: list[str] = []

        # 1. Brakes and permissions
        h = self.halted(now)
        if h:
            no.append(f"rem aktif: {h}")
        if cfg.mode == "live" and not self.j.get("go_live_confirmed"):
            no.append("mode live belum dikonfirmasi dengan 'go live' di Telegram")

        # 2. Strategy must have passed the Lab, recently, on this market
        lab = self.lab.get(sig.strategy)
        if not lab or not lab.get("passed"):
            no.append(f"strategi '{sig.strategy}' belum lulus uji Lab")
        else:
            age_days = (now - lab.get("tested_at", 0)) / 86400
            if age_days > LAB_RESULT_MAX_AGE_DAYS:
                no.append(f"hasil Lab '{sig.strategy}' sudah {age_days:.0f} hari, wajib uji ulang")
            if lab.get("symbol") != cfg.symbol or lab.get("timeframe") != cfg.timeframe:
                no.append("hasil Lab untuk market/timeframe lain")

        # 3. Data health
        age = now - m.price_ts
        if age > cfg.max_feed_age_seconds:
            no.append(f"data harga basi ({age:.0f} detik)")
        if m.equity_usdt <= 0 or m.price <= 0:
            no.append("saldo/harga dari exchange tidak valid")

        # 4. Exposure and frequency
        if abs(m.open_position_qty) > 0:
            no.append("sudah ada posisi terbuka (maks 1)")
        if len(self.j.trades_opened_since(utc_day_start(now))) >= cfg.max_trades_per_day:
            no.append(f"batas {cfg.max_trades_per_day} trade/hari tercapai")

        # 5. Signal sanity
        if sig.side == "LONG" and not (sig.stop < sig.entry < sig.target):
            no.append("stop/target LONG tidak masuk akal")
        if sig.side == "SHORT" and not (sig.target < sig.entry < sig.stop):
            no.append("stop/target SHORT tidak masuk akal")
        drift = abs(m.price - sig.entry)
        stop_dist = abs(sig.entry - sig.stop)
        if stop_dist > 0 and drift / stop_dist * 100 > cfg.max_entry_drift_pct_of_stop:
            no.append(f"harga sudah bergeser {drift / stop_dist * 100:.0f}% dari jarak stop (setup basi)")
        if sig.side == "LONG" and m.price <= sig.stop or sig.side == "SHORT" and m.price >= sig.stop:
            no.append("harga sudah melewati stop")

        if no or m.equity_usdt <= 0 or m.price <= 0 or stop_dist <= 0:
            return self._block(sig, no or ["input tidak valid"])

        # 6. Size from risk (fees + slippage counted as part of the loss)
        cost_pct = 2 * (cfg.taker_fee_pct + cfg.slippage_pct) / 100
        live_stop_dist = abs(m.price - sig.stop)
        risk_usdt = m.equity_usdt * cfg.risk_per_trade_pct / 100
        per_unit_loss = live_stop_dist + m.price * cost_pct
        qty = floor_step(risk_usdt / per_unit_loss, m.rules.step_size)

        # 7. Leverage cap: shrink size, never raise the cap
        margin_budget = m.equity_usdt * 0.9
        max_qty_by_lev = floor_step(margin_budget * cfg.max_leverage / m.price, m.rules.step_size)
        capped = qty > max_qty_by_lev
        qty = min(qty, max_qty_by_lev)
        notional = qty * m.price
        lev = max(1, math.ceil(notional / margin_budget)) if notional > 0 else 0

        if qty < m.rules.min_qty or notional < m.rules.min_notional:
            no.append(f"posisi terlalu kecil (${notional:,.2f}; minimum Binance ${m.rules.min_notional:,.0f}) — "
                      "modal kurang untuk stop sejauh ini")

        # 8. Fee guard
        fees = notional * 2 * cfg.taker_fee_pct / 100
        real_risk = qty * per_unit_loss
        if real_risk > 0 and fees / real_risk * 100 > cfg.max_fee_share_of_risk_pct:
            no.append(f"fee ${fees:.2f} = {fees / real_risk * 100:.0f}% dari risiko "
                      f"(batas {cfg.max_fee_share_of_risk_pct:.0f}%) — stop terlalu dekat")

        # 9. Stop must sit well inside the (approximate) liquidation distance
        if lev > 0:
            liq_dist_pct = 100 / lev - APPROX_MAINT_MARGIN_PCT
            if live_stop_dist / m.price * 100 > 0.7 * liq_dist_pct:
                no.append(f"stop terlalu dekat ke harga likuidasi (estimasi) pada {lev}x")

        if no:
            return self._block(sig, no)
        d = Decision(True, ["ukuran dipangkas karena batas leverage"] if capped else [],
                     qty, lev, round(real_risk, 2), round(notional, 2), round(fees, 2))
        self.j.log("shield", "shield_ok", d.text(), strategy=sig.strategy, side=sig.side)
        return d

    def _block(self, sig: Signal, reasons: list[str]) -> Decision:
        d = Decision(False, reasons)
        self.j.log("shield", "shield_block", d.text(), strategy=sig.strategy, side=sig.side)
        return d
