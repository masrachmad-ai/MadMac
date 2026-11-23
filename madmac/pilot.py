"""PILOT — sends orders. One job: turn an APPROVED + SHIELD-OK trade into a
position that is protected by a stop-loss living on Binance's servers.

Pilot refuses to act without both:
  * a Shield Decision with approved=True, and
  * a human approval id (or approval mode 'auto' that you set yourself).

Pilot also reconciles the journal with the exchange every loop, so a trade
closed by its stop/target is recorded, and a position without a stop is
never left alone.
"""
from __future__ import annotations

import time

from .binance import Binance, BinanceError, OrderUnknown
from .config import Config
from .journal import Journal
from .shield import Decision, Shield, floor_step
from .strategies import Signal


def trade_id(sig: Signal) -> str:
    """Deterministic: the same signal always maps to the same id, so it can
    never be executed twice (Binance also rejects a reused client id)."""
    return f"mm{sig.candle_time // 1000}{sig.strategy[:3]}{sig.side[0]}"


def round_tick(price: float, tick: float) -> float:
    return floor_step(price, tick)


class Pilot:
    def __init__(self, cfg: Config, ex: Binance, journal: Journal, shield: Shield, notify):
        self.cfg, self.ex, self.j, self.shield, self.notify = cfg, ex, journal, shield, notify
        self.tick = 0.1

    # ------------------------------------------------------------ entry
    def execute(self, sig: Signal, d: Decision, approved_by: str, tick: float) -> str | None:
        if not d.approved:
            raise PermissionError("Pilot dilarang mengirim order tanpa persetujuan Shield")
        if not approved_by:
            raise PermissionError("Pilot dilarang mengirim order tanpa izin Anda")
        tid = trade_id(sig)
        if self.j.get(f"exec:{tid}"):
            self.j.log("pilot", "duplicate_blocked", f"Sinyal {tid} sudah pernah dieksekusi — diabaikan.")
            return None
        self.j.set(f"exec:{tid}", {"ts": int(time.time()), "by": approved_by})

        sym = self.cfg.symbol
        entry_side = "BUY" if sig.side == "LONG" else "SELL"
        exit_side = "SELL" if sig.side == "LONG" else "BUY"

        self.ex.set_margin_type(sym, self.cfg.margin_type)
        self.ex.set_leverage(sym, d.leverage)

        try:
            o = self.ex.market_order(sym, entry_side, d.qty, tid)
        except OrderUnknown:
            self.shield.halt("Order entry timeout dan statusnya tidak bisa dipastikan. Cek posisi di Binance.")
            self.notify("🚨 Order entry tidak bisa dikonfirmasi. Bot DIHENTIKAN. Cek posisi di app Binance.")
            return None

        filled = float(o.get("executedQty") or 0)
        avg = float(o.get("avgPrice") or 0) or sig.entry
        if filled <= 0:
            self.j.log("pilot", "no_fill", f"Order {tid} tidak terisi (status {o.get('status')}).")
            self.notify(f"ℹ️ Order {sig.side} tidak terisi (status {o.get('status')}). Tidak ada posisi.")
            return None
        if filled < d.qty:
            self.j.log("pilot", "partial_fill", f"Hanya {filled}/{d.qty} terisi — stop melindungi posisi aktual.",
                       filled=filled, wanted=d.qty)

        self.j.open_trade(tid, self.cfg.mode, sig.strategy, sig.side, filled, avg,
                          sig.stop, sig.target, sig.reason)
        self.j.log("pilot", "entry", f"{sig.side} {filled} BTC @ {avg:,.1f}", tid=tid, approved_by=approved_by)

        self.tick = tick
        stop = round_tick(sig.stop, tick)
        target = round_tick(sig.target, tick)
        ok = self._protect(tid, exit_side, stop, target)
        msg = (f"✅ Masuk {sig.side} {filled} BTC @ {avg:,.1f}\n"
               f"Stop {stop:,.1f} · Target {target:,.1f} (keduanya terpasang di Binance)" if ok else
               f"⚠️ Masuk {sig.side} {filled} BTC @ {avg:,.1f}, TAPI stop gagal terpasang.")
        self.notify(msg)
        return tid

    def _protect(self, tid: str, exit_side: str, stop: float, target: float) -> bool:
        sym = self.cfg.symbol
        for attempt in range(2):
            try:
                if not self._has_stop():
                    self.ex.protective_order(sym, exit_side, "STOP_MARKET", stop, f"{tid}S{attempt}")
                if not self._has_type("TAKE_PROFIT_MARKET"):
                    self.ex.protective_order(sym, exit_side, "TAKE_PROFIT_MARKET", target, f"{tid}T{attempt}")
            except BinanceError as e:
                self.j.log("pilot", "protect_error", str(e), tid=tid)
            if self._has_stop():
                self.j.log("pilot", "protected", f"Stop {stop} terverifikasi di Binance.", tid=tid)
                return True
            time.sleep(1)
        self._unprotected(tid, exit_side)
        return False

    def _has_type(self, t: str) -> bool:
        return any(o["type"] == t for o in self.ex.open_protective_orders(self.cfg.symbol))

    def _has_stop(self) -> bool:
        return self._has_type("STOP_MARKET")

    def _unprotected(self, tid: str, exit_side: str) -> None:
        self.shield.halt("Posisi tanpa stop-loss terdeteksi.")
        if self.cfg.close_if_stop_fails:
            pos = self.ex.position(self.cfg.symbol)
            if pos["qty"] != 0:
                try:
                    self.ex.market_order(self.cfg.symbol, exit_side, abs(pos["qty"]), f"{tid}X", reduce_only=True)
                    self.j.log("pilot", "emergency_close", "Posisi ditutup karena stop tidak bisa dipasang.", tid=tid)
                    self.notify("🚨 Stop-loss gagal dipasang → posisi DITUTUP (izin close_if_stop_fails). Bot dihentikan.")
                    return
                except (BinanceError, OrderUnknown) as e:
                    self.j.log("pilot", "emergency_close_failed", str(e), tid=tid)
        self.notify("🚨🚨 POSISI TANPA STOP-LOSS. Bot dihentikan. Buka app Binance dan tutup/pasang stop SEKARANG.")

    # ------------------------------------------------------------ manual close
    def flatten(self, who: str) -> str:
        pos = self.ex.position(self.cfg.symbol)
        if pos["qty"] == 0:
            return "Tidak ada posisi terbuka."
        side = "SELL" if pos["qty"] > 0 else "BUY"
        self.ex.market_order(self.cfg.symbol, side, abs(pos["qty"]), f"mmflat{int(time.time())}", reduce_only=True)
        self.j.log("pilot", "manual_close", f"Posisi ditutup atas perintah {who}.")
        return f"Posisi {pos['qty']} BTC ditutup."

    # ------------------------------------------------------------ reconcile
    def reconcile(self) -> None:
        sym = self.cfg.symbol
        pos = self.ex.position(sym)
        open_tr = self.j.open_trades()

        if pos["qty"] == 0 and open_tr:
            for t in open_tr:
                self._record_close(t)
            self.ex.cancel_all(sym)  # leftover stop/TP of a closed position
            return

        if pos["qty"] != 0 and not open_tr:
            if not self.shield.halted():
                self.shield.halt("Ada posisi di Binance yang TIDAK dibuka bot ini.")
                self.notify(f"🚨 Posisi {pos['qty']} BTC di Binance bukan dari bot. Bot berhenti dan tidak menyentuhnya.")
            return

        if pos["qty"] != 0 and open_tr and not self._has_stop():
            t = open_tr[-1]
            exit_side = "SELL" if pos["qty"] > 0 else "BUY"
            self.j.log("pilot", "stop_missing", "Stop hilang dari Binance — memasang ulang.", tid=t["id"])
            self._protect(t["id"] + "R", exit_side, round_tick(t["stop"], self.tick), round_tick(t["target"], self.tick))

    def _record_close(self, t: dict) -> None:
        fills = self.ex.user_trades(self.cfg.symbol, t["opened_ts"] * 1000 - 1000)
        pnl = sum(float(f.get("realizedPnl", 0)) for f in fills)
        fees = sum(float(f.get("commission", 0)) for f in fills)  # USDT-margined → USDT
        exit_fills = [f for f in fills if float(f.get("realizedPnl", 0)) != 0]
        exit_px = (sum(float(f["price"]) * float(f["qty"]) for f in exit_fills) /
                   sum(float(f["qty"]) for f in exit_fills)) if exit_fills else 0.0
        net = pnl - fees
        why = "untung" if net >= 0 else "rugi"
        self.j.close_trade(t["id"], exit_px, round(net, 4), round(fees, 4), why)
        self.j.log("pilot", "exit", f"{t['side']} selesai @ {exit_px:,.1f}: {net:+.2f} USDT (fee {fees:.2f})", tid=t["id"])
        self.notify(f"{'🟢' if net >= 0 else '🔴'} Posisi {t['side']} selesai ({why}) @ {exit_px:,.1f} → "
                    f"{net:+.2f} USDT setelah fee.")
