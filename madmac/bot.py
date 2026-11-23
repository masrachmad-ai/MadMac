"""The team wired together. Runs forever; Docker restarts it after any crash.

Flow for every closed candle:
  Scout finds setup → Shield checks & sizes → Coordinator asks YOU →
  (you tap ✅) → Shield checks AGAIN with fresh data → Pilot executes →
  Journal records everything.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timezone

from .binance import Binance, BinanceError
from .config import DATA_DIR, Config
from .coordinator import WIB, Coordinator, Telegram, yesterday_start
from .journal import Journal, utc_day_start
from .pilot import Pilot, trade_id
from .scout import Scout, StaleFeed
from .shield import MarketSnapshot, Shield, SymbolRules, load_lab_results
from .strategies import Signal

LOOP_SECONDS = 5


class Bot:
    def __init__(self, cfg: Config, ex: Binance | None = None, journal: Journal | None = None,
                 tg: Telegram | None = None):
        self.cfg = cfg
        self.ex = ex or Binance(cfg.mode)
        self.j = journal or Journal(DATA_DIR / f"journal-{cfg.mode}.db")
        self.tg = tg or Telegram()
        self.shield = Shield(cfg, self.j)
        self.scout = Scout(cfg, self.ex, self.j)
        self.pilot = Pilot(cfg, self.ex, self.j, self.shield, self.tg.send)
        self.coord = Coordinator(self, self.tg, self.j)
        self.rules: SymbolRules | None = None
        self.api_failures = 0
        self.stale_alerted = False

    # -------------------------------------------------- data
    def snapshot(self) -> MarketSnapshot:
        price, ts = self.ex.price(self.cfg.symbol)
        return MarketSnapshot(self.ex.equity(), price, ts,
                              self.ex.position(self.cfg.symbol)["qty"], self.rules)

    # -------------------------------------------------- lifecycle
    def start(self) -> None:
        self.ex.sync_time()
        self.rules = self.ex.symbol_rules(self.cfg.symbol)
        self.shield.lab = load_lab_results()
        self.j.log("bot", "start", f"Bot mulai. Mode {self.cfg.mode}, {self.cfg.symbol} {self.cfg.timeframe}.")
        passed = [k for k, v in self.shield.lab.items() if v.get("passed")]
        self.tg.send(f"🤖 MadMac aktif · mode {self.cfg.mode.upper()} · {self.cfg.symbol} {self.cfg.timeframe}\n"
                     f"Strategi lulus Lab: {', '.join(passed) or 'BELUM ADA — bot hanya memantau'}\n"
                     f"Izin trade: {self.cfg.approval}. Ketik /help untuk perintah.")
        threading.Thread(target=self.coord.poll_forever, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self) -> None:
        """If the main loop freezes for 3 minutes, exit so Docker restarts us."""
        self.last_beat = time.time()
        while True:
            time.sleep(30)
            if time.time() - self.last_beat > 180:
                self.j.log("bot", "watchdog", "Loop utama macet >3 menit — restart.")
                self.tg.send("♻️ Bot macet >3 menit, restart otomatis.")
                os._exit(1)

    def run_forever(self) -> None:
        self.start()
        while True:
            try:
                self.tick()
                self.api_failures = 0
            except StaleFeed as e:
                self.j.log("scout", "stale_feed", str(e))
                if not self.stale_alerted:
                    self.tg.send(f"⚠️ Data harga basi ({e}). Tidak ada entry sampai data normal.")
                    self.stale_alerted = True
            except Exception as e:  # network, API, anything: log and keep going
                self.api_failures += 1
                self.j.log("bot", "error", f"{type(e).__name__}: {e}", tb=traceback.format_exc()[-1500:])
                if self.api_failures == 6:
                    self.tg.send(f"⚠️ Gangguan koneksi ke Binance ±30 detik ({type(e).__name__}). "
                                 "Entry baru otomatis diblokir. Stop-loss tetap aktif di Binance.")
                time.sleep(min(60, LOOP_SECONDS * self.api_failures))
            self.last_beat = time.time()
            (DATA_DIR / "heartbeat").write_text(str(int(self.last_beat)))
            time.sleep(LOOP_SECONDS)

    def tick(self) -> None:
        equity = self.ex.equity()
        self.shield.update_day(equity)
        self.pilot.reconcile()
        self.coord.expire()
        if self.coord.due_daily():
            self.tg.send(self.daily_report())
        if self.coord.due_weekly():
            self.tg.send(self.weekly_review())
        signals = self.scout.scan()
        self.stale_alerted = False
        for sig in signals:
            self.on_signal(sig)

    # -------------------------------------------------- decisions
    def on_signal(self, sig: Signal) -> None:
        d = self.shield.check(sig, self.snapshot())
        if not d.approved:
            return  # logged by Shield; shown in daily report
        if self.cfg.approval == "auto":
            self.on_approved(sig, approved_by="config:approval=auto")
        else:
            self.coord.ask_approval(trade_id(sig), sig, d)

    def on_approved(self, sig: Signal, approved_by: str) -> None:
        d = self.shield.check(sig, self.snapshot())  # re-check with FRESH data
        if not d.approved:
            self.tg.send(f"Shield membatalkan setelah persetujuan Anda: {'; '.join(d.reasons)}")
            return
        try:
            self.pilot.execute(sig, d, approved_by, self.rules.tick_size)
        except BinanceError as e:
            self.j.log("pilot", "order_error", str(e))
            self.tg.send(f"❌ Binance menolak order: {e.msg}")

    # -------------------------------------------------- reports
    def _pos_text(self) -> str:
        pos = self.ex.position(self.cfg.symbol)
        if pos["qty"] == 0:
            return "Tidak ada posisi terbuka."
        price, _ = self.ex.price(self.cfg.symbol)
        upnl = (price - pos["entry"]) * pos["qty"]
        return f"Posisi {pos['qty']} BTC @ {pos['entry']:,.1f} · harga {price:,.1f} · floating {upnl:+.2f} USDT"

    def status_text(self) -> str:
        halt = self.shield.halted()
        return (f"Mode {self.cfg.mode.upper()} · Equity {self.ex.equity():,.2f} USDT\n"
                f"{self._pos_text()}\n"
                f"P&L hari ini (terealisasi): {self.j.realized_pnl_today():+.2f} USDT · "
                f"rugi beruntun: {self.j.consecutive_losses()}\n"
                f"Rem: {'🛑 ' + halt if halt else '✅ tidak aktif'}")

    def daily_report(self) -> str:
        since = yesterday_start()
        until = utc_day_start()
        c = self.ex.klines(self.cfg.symbol, "1h", 25)
        day = [x for x in c if since * 1000 <= x.open_time < until * 1000] or c[-24:]
        market = (f"Pasar: BTC {(day[-1].close / day[0].open - 1) * 100:+.2f}% · high {max(x.high for x in day):,.0f} · "
                  f"low {min(x.low for x in day):,.0f} (sumber: Binance klines 1h)") if day else \
                 "Pasar: data tidak tersedia dari Binance saat laporan dibuat"
        s = self.j.summary(since)
        ev = self.j.events_since(since)
        n_sig = sum(e["kind"] == "signal" for e in ev)
        n_ok = sum(e["kind"] == "approved" for e in ev)
        n_exp = sum(e["kind"] == "expired" for e in ev)
        blocks = [e["message"] for e in ev if e["kind"] == "shield_block"]
        halts = [e["message"] for e in ev if e["kind"] == "halt"]
        decision = self._decision_needed(s, n_exp)
        date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%d %b %Y")
        return "\n".join([
            f"📊 LAPORAN HARIAN {date} (UTC) · mode {self.cfg.mode.upper()}",
            market,
            f"Scout: {n_sig} sinyal · Anda setujui {n_ok} · kedaluwarsa {n_exp}",
            f"Shield: memblokir {len(blocks)}" + (f" — contoh: {blocks[-1][9:120]}" if blocks else ""),
            f"Pilot: {s['trades']} trade selesai · win {s['wins']} · P&L {s['pnl']:+.2f} USDT (fee {s['fees']:.2f})",
            self._pos_text(),
            ("Rem: " + " | ".join(h[:100] for h in halts)) if halts else "Rem: tidak terpicu",
            f"➡️ Keputusan untuk Anda: {decision}",
        ])

    def _decision_needed(self, s: dict, expired: int) -> str:
        if self.shield.halted():
            return "Bot sedang berhenti. Tinjau trade terakhir lalu ketik /resume, atau biarkan berhenti."
        if not any(v.get("passed") for v in self.shield.lab.values()):
            return "Belum ada strategi lulus Lab. Bot hanya memantau. Tidak ada yang perlu Anda lakukan."
        if expired >= 3:
            return f"{expired} sinyal kedaluwarsa tanpa jawaban. Perpanjang approval_timeout_seconds atau aktifkan notifikasi Telegram?"
        return "Tidak ada. Cukup baca laporan ini."

    def weekly_review(self) -> str:
        s = self.j.summary(self.j.week_start())
        strat = "\n".join(f"  · {k}: {v:+.2f} USDT" for k, v in s["by_strategy"].items()) or "  · belum ada trade"
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] else "–"
        return (f"🗓️ REVIEW MINGGUAN · mode {self.cfg.mode.upper()}\n"
                f"Trade {s['trades']} · win rate {s['win_rate']:.0f}% · profit factor {pf}\n"
                f"P&L {s['pnl']:+.2f} USDT setelah fee · total fee {s['fees']:.2f}\n"
                f"Diblokir Shield: {s['blocked']}\nPer strategi:\n{strat}\n"
                "Catatan: kurang dari ±30 trade = terlalu sedikit untuk menyimpulkan apa pun.")

    def lab_text(self) -> str:
        if not self.shield.lab:
            return "Lab belum pernah dijalankan. Lihat docs/DEPLOY.md langkah 6."
        out = []
        for k, v in self.shield.lab.items():
            o = v["out_of_sample"]
            out.append(f"{'✅' if v['passed'] else '❌'} {k}: {o['trades']} trade, PF {o['profit_factor']}, "
                       f"return {o['return_pct']}%, DD {o['max_dd_pct']:.1f}% (out-of-sample, setelah fee)")
            if v["why_not"]:
                out.append("   alasan: " + "; ".join(v["why_not"]))
        tested = datetime.fromtimestamp(max(v["tested_at"] for v in self.shield.lab.values()), tz=WIB)
        return "\n".join(out + [f"Diuji: {tested:%d %b %Y %H:%M} WIB"])
