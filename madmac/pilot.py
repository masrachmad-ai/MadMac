"""PILOT — sends orders. One job: turn an APPROVED + SHIELD-OK trade into a
position protected by a stop-loss living on Binance's servers.

Pilot refuses to act without both:
  * a Shield Decision with approved=True, and
  * an approval id (your ✅ tap, or the approval: auto you set in config).

Maker flow:  post-only limit at the signal price → (every 5 s) check fill →
             stop-loss as soon as ANY quantity is filled → after fill/timeout,
             take-profit (reduce-only limit) for the filled quantity.
Taker flow:  market order → stop + take-profit immediately.

Every loop Pilot also reconciles the journal with Binance: closed positions
are recorded from Binance's own fills, missing stops are replaced, trades
past their time limit are closed (part of the strategy's plan), and unknown
positions trigger the brake.
"""
from __future__ import annotations

import time

from .binance import BinanceError, OrderUnknown
from .config import Config
from .journal import Journal
from .shield import Decision, Shield, SymbolRules, floor_step


def trade_id(sig) -> str:
    """Deterministic and ≤ 36 chars: the same signal always maps to the same
    id, so it can never be executed twice (Binance rejects a reused id)."""
    sym = sig.symbol.replace("USDT", "")[:8]
    return f"mm{sig.candle_time // 1000}{sym}{sig.strategy[:3]}{sig.side[0]}"


def round_tick(price: float, tick: float) -> float:
    return floor_step(price, tick)


class Pilot:
    def __init__(self, cfg: Config, ex, journal: Journal, shield: Shield, notify, info=None):
        self.cfg, self.ex, self.j, self.shield = cfg, ex, journal, shield
        self.notify = notify                     # alarms: always sent
        self.info = info or (lambda text: None)  # routine trade messages
        self.rules: dict[str, SymbolRules] = {}

    def _tick(self, sym: str) -> float:
        r = self.rules.get(sym)
        return r.tick_size if r else 0.0

    # ------------------------------------------------------------ entry
    def execute(self, sig, d: Decision, approved_by: str) -> str | None:
        if not d.approved:
            raise PermissionError("Pilot dilarang mengirim order tanpa persetujuan Shield")
        if not approved_by:
            raise PermissionError("Pilot dilarang mengirim order tanpa izin Anda")
        tid = trade_id(sig)
        if self.j.get(f"exec:{tid}"):
            self.j.log("pilot", "duplicate_blocked", f"Sinyal {tid} sudah pernah dieksekusi — diabaikan.")
            return None
        self.j.set(f"exec:{tid}", {"ts": int(time.time()), "by": approved_by})

        sym = sig.symbol
        tick = self._tick(sym)
        side = "BUY" if sig.side == "LONG" else "SELL"
        hold_s = sig.max_hold * self.cfg.tf_seconds
        self.ex.set_margin_type(sym, self.cfg.margin_type)
        self.ex.set_leverage(sym, d.leverage)

        try:
            if self.cfg.entry_order == "maker":
                px = round_tick(sig.entry, tick)
                self.j.add_trade(tid, sym, self.cfg.mode, sig.strategy, sig.side, d.qty, px,
                                 round_tick(sig.stop, tick), round_tick(sig.target, tick), sig.reason, hold_s)
                o = self.ex.limit_order(sym, side, d.qty, px, tid)
                self.j.log("pilot", "entry_placed", f"{sym} {sig.side} limit {d.qty} @ {px}",
                           tid=tid, approved_by=approved_by)
                self._handle_order(self.j.trade(tid), o, final=False)
            else:
                o = self.ex.market_order(sym, side, d.qty, tid)
                self.j.add_trade(tid, sym, self.cfg.mode, sig.strategy, sig.side, d.qty, sig.entry,
                                 round_tick(sig.stop, tick), round_tick(sig.target, tick), sig.reason, hold_s)
                self.j.log("pilot", "entry_placed", f"{sym} {sig.side} market {d.qty}",
                           tid=tid, approved_by=approved_by)
                self._handle_order(self.j.trade(tid), o, final=True)
        except OrderUnknown:
            self.shield.halt(f"Order entry {sym} timeout dan statusnya tidak bisa dipastikan.")
            self.notify(f"🚨 Order {sym} tidak bisa dikonfirmasi. Bot DIHENTIKAN. Cek posisi di app Binance.")
            return None
        return tid

    def _handle_order(self, t: dict | None, o: dict, final: bool) -> None:
        """Act on an entry order's state. final=True means no more fills can come."""
        if not t:
            return
        filled = float(o.get("executedQty") or 0)
        status = o.get("status")
        done = final or status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED")
        if filled > 0:
            avg = float(o.get("avgPrice") or 0) or t["entry"]
            exit_side = "SELL" if t["side"] == "LONG" else "BUY"
            # Stop goes on as soon as ANY quantity is filled, even mid-fill.
            if not self._protect_stop(t, exit_side):
                if done:
                    self.j.mark_open(t["id"], filled, avg)
                return
            if done and not t["tp_placed"]:
                self.j.mark_open(t["id"], filled, avg)
                if filled < t["qty"] and t["status"] == "pending":
                    self.j.log("pilot", "partial_fill", f"{t['symbol']}: hanya {filled}/{t['qty']} terisi.",
                               tid=t["id"])
                self._place_tp(t, exit_side, filled)
                self.info(f"✅ {t['symbol']} {t['side']} {filled} @ {avg:,.6g} · stop {t['stop']:,.6g} · "
                          f"target {t['target']:,.6g} ({t['strategy']})")
        elif done:
            self.j.mark_cancelled(t["id"], f"limit tidak terisi ({status})")
            self.j.log("pilot", "unfilled", f"{t['symbol']} limit tidak terisi ({status}) — tidak ada posisi.",
                       tid=t["id"])

    # ------------------------------------------------------------ protection
    def _protect_stop(self, t: dict, exit_side: str) -> bool:
        sym = t["symbol"]
        for attempt in range(2):
            try:
                if self._has(sym, "STOP_MARKET"):
                    return True
                self.ex.protective_order(sym, exit_side, "STOP_MARKET", t["stop"], f"{t['id']}S{attempt}")
            except BinanceError as e:
                self.j.log("pilot", "protect_error", f"{sym}: {e}", tid=t["id"])
            if self._has(sym, "STOP_MARKET"):
                self.j.log("pilot", "protected", f"{sym} stop {t['stop']} terverifikasi di Binance.", tid=t["id"])
                return True
            time.sleep(1)
        self._unprotected(t, exit_side)
        return False

    def _place_tp(self, t: dict, exit_side: str, qty: float) -> None:
        sym = t["symbol"]
        try:
            o = self.ex.limit_order(sym, exit_side, qty, t["target"], f"{t['id']}T", reduce_only=True)
            if o.get("status") == "EXPIRED":   # price already beyond target → would take
                raise BinanceError(400, None, "post-only TP rejected")
        except BinanceError:
            try:
                self.ex.protective_order(sym, exit_side, "TAKE_PROFIT_MARKET", t["target"], f"{t['id']}P")
            except BinanceError as e:
                self.j.log("pilot", "tp_error", f"{sym}: {e} (stop tetap aktif)", tid=t["id"])
                return
        self.j.mark_tp(t["id"])

    def _has(self, sym: str, typ: str) -> bool:
        return any(o["type"] == typ for o in self.ex.open_protective_orders(sym))

    def _unprotected(self, t: dict, exit_side: str) -> None:
        sym = t["symbol"]
        self.shield.halt(f"Posisi {sym} tanpa stop-loss terdeteksi.")
        if self.cfg.close_if_stop_fails:
            pos = self.ex.position(sym)
            if pos["qty"] != 0:
                try:
                    self.ex.cancel_order(sym, t["id"])
                    self.ex.market_order(sym, exit_side, abs(pos["qty"]), f"{t['id']}X", reduce_only=True)
                    self.j.log("pilot", "emergency_close", f"{sym} ditutup: stop tidak bisa dipasang.", tid=t["id"])
                    self.notify(f"🚨 Stop-loss {sym} gagal dipasang → posisi DITUTUP (izin close_if_stop_fails). "
                                "Bot dihentikan.")
                    return
                except (BinanceError, OrderUnknown) as e:
                    self.j.log("pilot", "emergency_close_failed", f"{sym}: {e}", tid=t["id"])
        self.notify(f"🚨🚨 POSISI {sym} TANPA STOP-LOSS. Bot dihentikan. Buka app Binance SEKARANG.")

    # ------------------------------------------------------------ manual close
    def flatten(self, who: str) -> str:
        pos = self.ex.positions()
        out = []
        for t in self.j.active_trades():
            if t["status"] == "pending":
                self.ex.cancel_order(t["symbol"], t["id"])
        for sym, p in pos.items():
            if sym not in self.cfg.symbols:
                continue
            side = "SELL" if p["qty"] > 0 else "BUY"
            self.ex.market_order(sym, side, abs(p["qty"]), f"mmflat{int(time.time())}{sym[:6]}", reduce_only=True)
            out.append(f"{sym} {p['qty']}")
        self.j.log("pilot", "manual_close", f"Semua posisi ditutup atas perintah {who}: {out}")
        return "Ditutup: " + ", ".join(out) if out else "Tidak ada posisi terbuka."

    # ------------------------------------------------------------ reconcile
    def reconcile(self, positions: dict[str, dict] | None = None, now: float | None = None) -> None:
        now = now or time.time()
        pos = positions if positions is not None else self.ex.positions()
        timeout_s = self.cfg.entry_timeout_candles * self.cfg.tf_seconds

        for t in self.j.active_trades():
            sym = t["symbol"]
            qty_now = pos.get(sym, {}).get("qty", 0.0)
            exit_side = "SELL" if t["side"] == "LONG" else "BUY"

            if t["status"] == "pending":
                o = self.ex.get_order(sym, t["id"])
                expired = now - t["placed_ts"] > timeout_s
                if expired and o.get("status") in ("NEW", "PARTIALLY_FILLED"):
                    self.ex.cancel_order(sym, t["id"])
                    o = self.ex.get_order(sym, t["id"])  # re-read: a fill may have raced the cancel
                self._handle_order(t, o, final=expired)
                continue

            if qty_now == 0:
                self._record_close(t)
                self.ex.cancel_all(sym)
                continue
            if not self._has(sym, "STOP_MARKET"):
                self.j.log("pilot", "stop_missing", f"{sym}: stop hilang dari Binance — memasang ulang.", tid=t["id"])
                if not self._protect_stop(t, exit_side):
                    continue
            if t["max_hold_s"] and t["opened_ts"] and now - t["opened_ts"] > t["max_hold_s"]:
                try:
                    self.ex.cancel_all(sym)
                    self.ex.market_order(sym, exit_side, abs(qty_now), f"{t['id']}H", reduce_only=True)
                    self.j.log("pilot", "time_exit", f"{sym}: batas waktu strategi habis — ditutup.", tid=t["id"])
                except (BinanceError, OrderUnknown) as e:
                    self.j.log("pilot", "time_exit_error", f"{sym}: {e}", tid=t["id"])

        known = {t["symbol"] for t in self.j.active_trades()}
        strangers = [s for s, p in pos.items() if s in self.cfg.symbols and s not in known and p["qty"] != 0]
        if strangers and not self.shield.halted():
            self.shield.halt(f"Ada posisi di Binance yang TIDAK dibuka bot: {strangers}")
            self.notify(f"🚨 Posisi {strangers} di Binance bukan dari bot. Bot berhenti dan tidak menyentuhnya. "
                        "Saran: pakai sub-account khusus untuk bot.")

    def _record_close(self, t: dict) -> None:
        sym = t["symbol"]
        fills = self.ex.user_trades(sym, (t["opened_ts"] or t["placed_ts"]) * 1000 - 1000)
        pnl = sum(float(f.get("realizedPnl", 0)) for f in fills)
        fees = sum(float(f.get("commission", 0)) for f in fills)  # USDT-margined → USDT
        exits = [f for f in fills if float(f.get("realizedPnl", 0)) != 0]
        exit_px = (sum(float(f["price"]) * float(f["qty"]) for f in exits) /
                   sum(float(f["qty"]) for f in exits)) if exits else 0.0
        net = pnl - fees
        self.j.close_trade(t["id"], exit_px, round(net, 4), round(fees, 4), "untung" if net >= 0 else "rugi")
        self.j.log("pilot", "exit", f"{sym} {t['side']} selesai @ {exit_px:,.6g}: {net:+.2f} USDT (fee {fees:.2f})",
                   tid=t["id"])
        self.info(f"{'🟢' if net >= 0 else '🔴'} {sym} {t['side']} selesai @ {exit_px:,.6g} → {net:+.2f} USDT setelah fee.")
