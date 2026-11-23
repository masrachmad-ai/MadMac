"""SHIELD — the risk manager. One job: say YES or NO to a trade, and size it.

Shield never sends orders. It only returns a Decision. Pilot is not allowed
to send an entry order without an approved Decision. Shield also owns the
emergency brake (halt) that stops all new entries in seconds.

Every number Shield uses comes from the exchange (equity, price, filters)
or the Journal (open risk, today's results). If any input is missing or
stale, the answer is NO.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR, Config
from .journal import Journal, utc_day_start

# Approximate maintenance margin, lowest bracket. ESTIMATE — used only to keep
# the stop well away from liquidation.
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
    price: float                 # latest price of THIS symbol
    price_ts: float              # unix seconds when that price was observed
    open_position_qty: float     # signed, this symbol, from exchange (0 = flat)
    rules: SymbolRules


@dataclass
class Decision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    qty: float = 0.0
    leverage: int = 0
    risk_usdt: float = 0.0
    notional_usdt: float = 0.0
    est_fees_usdt: float = 0.0

    def text(self) -> str:
        if self.approved:
            extra = f" ({'; '.join(self.reasons)})" if self.reasons else ""
            return (f"Shield ✅ qty {self.qty} · leverage {self.leverage}x · risiko ${self.risk_usdt:.2f} · "
                    f"nilai posisi ${self.notional_usdt:,.0f} · fee est. ${self.est_fees_usdt:.2f}{extra}")
        return "Shield ⛔ " + "; ".join(self.reasons)


def floor_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    n = math.floor(x / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals)


def lab_key(strategy: str, symbol: str) -> str:
    return f"{strategy}|{symbol}"


def load_lab_results(path: Path | None = None) -> dict:
    p = path or DATA_DIR / "lab_results.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text()).get("pairs", {})


class Shield:
    def __init__(self, cfg: Config, journal: Journal, lab_results: dict | None = None):
        self.cfg = cfg
        self.j = journal
        self.lab = lab_results if lab_results is not None else load_lab_results()

    def passed_pairs(self, now: float | None = None) -> set[tuple[str, str]]:
        now = now or time.time()
        out = set()
        for k, v in self.lab.items():
            if (v.get("passed") and v.get("timeframe") == self.cfg.timeframe
                    and (now - v.get("tested_at", 0)) / 86400 <= LAB_RESULT_MAX_AGE_DAYS
                    and v["strategy"] in self.cfg.strategies and v["symbol"] in self.cfg.symbols):
                out.add((v["strategy"], v["symbol"]))
        return out

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
        day = utc_day_start(now)
        rec = self.j.get("day_start")
        if not rec or rec["day"] != day:
            rec = {"day": day, "equity": equity}
            self.j.set("day_start", rec)
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
    def check(self, sig, m: MarketSnapshot, now: float | None = None) -> Decision:
        now = now or time.time()
        cfg = self.cfg
        no: list[str] = []
        sym = sig.symbol

        # 1. Brakes and permissions
        h = self.halted(now)
        if h:
            no.append(f"rem aktif: {h}")
        if cfg.mode == "live" and not self.j.get("go_live_confirmed"):
            no.append("mode live belum dikonfirmasi dengan 'go live' di Telegram")

        # 2. This exact strategy+coin must have passed the Lab, recently
        if (sig.strategy, sym) not in self.passed_pairs(now):
            no.append(f"{sig.strategy} di {sym} belum lulus uji Lab (atau hasilnya > {LAB_RESULT_MAX_AGE_DAYS} hari)")

        # 3. Data health
        age = now - m.price_ts
        if age > cfg.max_feed_age_seconds:
            no.append(f"data harga basi ({age:.0f} detik)")
        if m.equity_usdt <= 0 or m.price <= 0:
            no.append("saldo/harga dari exchange tidak valid")

        # 4. Portfolio exposure
        active = self.j.active_trades()
        if abs(m.open_position_qty) > 0 or any(t["symbol"] == sym for t in active):
            no.append(f"sudah ada posisi/order di {sym}")
        if len(active) >= cfg.max_open_positions:
            no.append(f"sudah {len(active)} posisi/order aktif (maks {cfg.max_open_positions})")
        same = sum(t["side"] == sig.side for t in active)
        if same >= cfg.max_same_direction:
            no.append(f"sudah {same} posisi {sig.side} (maks {cfg.max_same_direction} searah — korelasi)")
        if len(self.j.trades_opened_since(utc_day_start(now))) >= cfg.max_trades_per_day:
            no.append(f"batas {cfg.max_trades_per_day} trade/hari tercapai")

        # 5. Signal sanity
        if sig.side == "LONG" and not (sig.stop < sig.entry < sig.target):
            no.append("stop/target LONG tidak masuk akal")
        if sig.side == "SHORT" and not (sig.target < sig.entry < sig.stop):
            no.append("stop/target SHORT tidak masuk akal")
        stop_dist = abs(sig.entry - sig.stop)
        drift = abs(m.price - sig.entry)
        if stop_dist > 0 and drift / stop_dist * 100 > cfg.max_entry_drift_pct_of_stop:
            no.append(f"harga sudah bergeser {drift / stop_dist * 100:.0f}% dari jarak stop (setup basi)")
        if (sig.side == "LONG" and m.price <= sig.stop) or (sig.side == "SHORT" and m.price >= sig.stop):
            no.append("harga sudah melewati stop")

        if no or stop_dist <= 0:
            return self._block(sig, no or ["input tidak valid"])

        # 6. Size from risk. Entry at the signal price (maker) or current price (taker).
        entry = sig.entry if cfg.entry_order == "maker" else m.price
        fee_in, fee_out, slip = cfg.entry_fee_pct / 100, cfg.taker_fee_pct / 100, cfg.slippage_pct / 100
        live_stop = abs(entry - sig.stop)
        per_unit_loss = live_stop + entry * fee_in + sig.stop * (fee_out + slip)
        risk_budget = m.equity_usdt * cfg.risk_per_trade_pct / 100
        open_risk = sum(t["qty"] * abs(t["entry"] - t["stop"]) for t in active)
        room = m.equity_usdt * cfg.max_total_open_risk_pct / 100 - open_risk
        notes = []
        if room < risk_budget:
            if room < 0.5 * risk_budget:
                return self._block(sig, [f"total risiko terbuka sudah ${open_risk:.2f} "
                                         f"(maks {cfg.max_total_open_risk_pct}% modal)"])
            risk_budget = room
            notes.append("ukuran dikecilkan: batas total risiko")
        qty = floor_step(risk_budget / per_unit_loss, m.rules.step_size)

        # 7. Leverage cap: shrink size, never raise the cap
        margin_budget = m.equity_usdt * 0.9 / cfg.max_open_positions
        max_qty_by_lev = floor_step(margin_budget * cfg.max_leverage / entry, m.rules.step_size)
        if qty > max_qty_by_lev:
            qty = max_qty_by_lev
            notes.append("ukuran dikecilkan: batas leverage")
        notional = qty * entry
        lev = max(1, math.ceil(notional / margin_budget)) if notional > 0 else 0

        if qty < m.rules.min_qty or notional < m.rules.min_notional:
            no.append(f"posisi terlalu kecil (${notional:,.2f}; minimum Binance ${m.rules.min_notional:,.0f})")

        # 8. Fee guard (entry fee + taker exit fee vs the money at risk)
        fees = notional * (fee_in + fee_out)
        real_risk = qty * per_unit_loss
        if real_risk > 0 and fees / real_risk * 100 > cfg.max_fee_share_of_risk_pct:
            no.append(f"fee ${fees:.2f} = {fees / real_risk * 100:.0f}% dari risiko "
                      f"(batas {cfg.max_fee_share_of_risk_pct:.0f}%) — stop terlalu dekat")

        # 9. Stop must sit well inside the (approximate) liquidation distance
        if lev > 0 and live_stop / entry * 100 > 0.7 * (100 / lev - APPROX_MAINT_MARGIN_PCT):
            no.append(f"stop terlalu dekat ke harga likuidasi (estimasi) pada {lev}x")

        if no:
            return self._block(sig, no)
        d = Decision(True, notes, qty, lev, round(real_risk, 2), round(notional, 2), round(fees, 2))
        self.j.log("shield", "shield_ok", d.text(), strategy=sig.strategy, side=sig.side, symbol=sym)
        return d

    def _block(self, sig, reasons: list[str]) -> Decision:
        d = Decision(False, reasons)
        self.j.log("shield", "shield_block", f"{sig.symbol} {sig.strategy}: {d.text()}",
                   strategy=sig.strategy, side=sig.side, symbol=sig.symbol)
        return d
