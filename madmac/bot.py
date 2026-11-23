"""The team wired together. Runs forever; Docker restarts it after any crash.

Every 5 seconds:
  Pilot reconciles positions with Binance (fills, stops, exits, time limits)
  Shield updates the daily brakes
  Scout checks coins whose candle just closed (only Lab-approved pairs)
  For each signal: Shield checks & sizes → (auto, or your ✅) → Shield checks
  AGAIN with fresh data → Pilot places the order → Journal records it.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone

from .binance import Binance, BinanceError
from .config import DATA_DIR, Config
from .coordinator import WIB, Coordinator, Telegram, yesterday_start
from .journal import Journal, utc_day_start
from .pilot import Pilot, trade_id
from .scout import Scout
from .shield import MarketSnapshot, Shield, load_lab_results

LOOP_SECONDS = 5


class Bot:
    def __init__(self, cfg: Config, ex: Binance | None = None, journal: Journal | None = None,
                 tg: Telegram | None = None, market: Binance | None = None):
        self.cfg = cfg
        self.ex = ex or Binance(cfg.mode)                  # orders + account
        self.mkt = market or (self.ex if ex else Binance("live", key="", secret=""))  # real market data
        self.j = journal or Journal(DATA_DIR / f"journal-{cfg.mode}.db")
        self.tg = tg or Telegram()
        self.shield = Shield(cfg, self.j)
        self.scout = Scout(cfg, self.mkt, self.j)
        self.pilot = Pilot(cfg, self.ex, self.j, self.shield, notify=self.alarm, info=self.info)
        self.coord = Coordinator(self, self.tg, self.j)
        self.api_failures = 0
        self.last_beat = time.time()
        self.lab_mtime = 0.0

    # -------------------------------------------------- messaging
    def alarm(self, text: str) -> None:
        self.tg.send(text)

    def info(self, text: str) -> None:
        if self.cfg.notify_each_trade:
            self.tg.send(text)

    # -------------------------------------------------- data
    def snapshot(self, sym: str, equity: float | None = None, prices: dict | None = None,
                 positions: dict | None = None) -> MarketSnapshot:
        prices = prices if prices is not None else self.ex.prices()
        positions = positions if positions is not None else self.ex.positions()
        price, ts = prices.get(sym, (0.0, 0.0))
        return MarketSnapshot(equity if equity is not None else self.ex.equity(), price, ts,
                              positions.get(sym, {}).get("qty", 0.0), self.pilot.rules[sym])

    def reload_lab(self) -> None:
        p = DATA_DIR / "lab_results.json"
        m = p.stat().st_mtime if p.exists() else 0.0
        if m != self.lab_mtime:
            self.lab_mtime = m
            self.shield.lab = load_lab_results(p)
            self.j.log("bot", "lab_loaded", f"Hasil Lab dimuat: {len(self.shield.passed_pairs())} pasangan lulus.")

    # -------------------------------------------------- lifecycle
    def start(self) -> None:
        self.ex.sync_time()
        self.pilot.rules = self.ex.symbol_rules_all(self.cfg.symbols)
        missing = [s for s in self.cfg.symbols if s not in self.pilot.rules]
        self.reload_lab()
        pairs = sorted(self.shield.passed_pairs())
        self.j.log("bot", "start", f"Bot mulai. Mode {self.cfg.mode}, {len(self.cfg.symbols)} koin {self.cfg.timeframe}.")
        self.tg.send(
            f"🤖 MadMac aktif · mode {self.cfg.mode.upper()} · {len(self.cfg.symbols)} koin · {self.cfg.timeframe}\n"
            f"Pasangan lulus Lab: {len(pairs)}" + (f" ({', '.join(f'{a}/{b}' for a, b in pairs[:8])}"
                                                    f"{'…' if len(pairs) > 8 else ''})" if pairs else
                                                    " — bot hanya memantau") +
            f"\nIzin trade: {self.cfg.approval} · entry {self.cfg.entry_order}"
            + (f"\n⚠️ Tidak ada di exchange ini: {missing}" if missing else "")
            + "\nKetik /help untuk perintah.")
        threading.Thread(target=self.coord.poll_forever, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self) -> None:
        """If the main loop freezes for 3 minutes, exit so Docker restarts us."""
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
            except Exception as e:  # network, API, anything: log and keep going
                self.api_failures += 1
                self.j.log("bot", "error", f"{type(e).__name__}: {e}", tb=traceback.format_exc()[-1500:])
                if self.api_failures == 6:
                    self.tg.send(f"⚠️ Gangguan koneksi ke Binance ±30 detik ({type(e).__name__}). "
                                 "Entry baru tertahan. Stop-loss tetap aktif di Binance.")
                time.sleep(min(60, LOOP_SECONDS * self.api_failures))
            self.last_beat = time.time()
            (DATA_DIR / "heartbeat").write_text(str(int(self.last_beat)))
            time.sleep(LOOP_SECONDS)

    def tick(self) -> None:
        equity = self.ex.equity()
        self.shield.update_day(equity)
        positions = self.ex.positions()
        self.pilot.reconcile(positions)
        self.coord.expire()
        self.reload_lab()
        if self.coord.due_daily():
            self.tg.send(self.daily_report())
        if self.coord.due_weekly():
            self.tg.send(self.weekly_review())
        signals = self.scout.scan(self.shield.passed_pairs())
        if signals:
            prices = self.ex.prices()
            for sig in signals:
                self.on_signal(sig, equity, prices, self.ex.positions())

    # -------------------------------------------------- decisions
    def anchor(self, sig, prices: dict):
        """Testnet prices drift away from the real market. Signals come from
        real data, so on testnet shift entry/stop/target by the price gap
        (same distances, same risk). No-op on live."""
        if self.cfg.mode != "testnet" or sig.symbol not in prices:
            return sig
        gap = prices[sig.symbol][0] - sig.entry
        return replace(sig, entry=sig.entry + gap, stop=sig.stop + gap, target=sig.target + gap)

    def on_signal(self, sig, equity: float, prices: dict, positions: dict) -> None:
        if sig.symbol not in self.pilot.rules:
            return
        sig = self.anchor(sig, prices)
        d = self.shield.check(sig, self.snapshot(sig.symbol, equity, prices, positions))
        if not d.approved:
            return  # logged by Shield; shown in daily report
        if self.cfg.approval == "auto":
            self.on_approved(sig, approved_by="config:approval=auto")
        else:
            self.coord.ask_approval(trade_id(sig), sig, d)

    def on_approved(self, sig, approved_by: str) -> None:
        d = self.shield.check(sig, self.snapshot(sig.symbol))  # re-check with FRESH data
        if not d.approved:
            if approved_by.startswith("telegram"):
                self.tg.send(f"Shield membatalkan setelah persetujuan Anda: {'; '.join(d.reasons)}")
            return
        try:
            self.pilot.execute(sig, d, approved_by)
        except BinanceError as e:
            self.j.log("pilot", "order_error", f"{sig.symbol}: {e}")
            self.alarm(f"❌ Binance menolak order {sig.symbol}: {e.msg}")

    # -------------------------------------------------- reports
    def _pos_lines(self) -> list[str]:
        pos = self.ex.positions()
        prices = self.ex.prices() if pos else {}
        lines = []
        for sym, p in pos.items():
            px = prices.get(sym, (p["entry"], 0))[0]
            lines.append(f"· {sym} {p['qty']:+} @ {p['entry']:,.6g} → {px:,.6g} · floating "
                         f"{(px - p['entry']) * p['qty']:+.2f} USDT")
        pend = [t for t in self.j.active_trades() if t["status"] == "pending"]
        if pend:
            lines.append(f"· {len(pend)} limit order menunggu terisi")
        return lines or ["Tidak ada posisi terbuka."]

    def status_text(self) -> str:
        halt = self.shield.halted()
        return "\n".join([f"Mode {self.cfg.mode.upper()} · Equity {self.ex.equity():,.2f} USDT",
                          *self._pos_lines(),
                          f"P&L hari ini (terealisasi): {self.j.realized_pnl_today():+.2f} USDT · "
                          f"rugi beruntun: {self.j.consecutive_losses()}",
                          f"Pasangan aktif (lulus Lab): {len(self.shield.passed_pairs())}",
                          f"Rem: {'🛑 ' + halt if halt else '✅ tidak aktif'}"])

    def _market_line(self, since: int, until: int) -> str:
        moves = []
        for sym in self.cfg.symbols:
            try:
                c = [x for x in self.mkt.klines(sym, "1h", 30) if since * 1000 <= x.open_time < until * 1000]
            except Exception:
                continue
            if c:
                moves.append((sym, (c[-1].close / c[0].open - 1) * 100))
        if not moves:
            return "Pasar: data tidak tersedia dari Binance saat laporan dibuat"
        btc = next((m for s, m in moves if s == "BTCUSDT"), None)
        best, worst = max(moves, key=lambda x: x[1]), min(moves, key=lambda x: x[1])
        return ((f"Pasar: BTC {btc:+.2f}% · " if btc is not None else "Pasar: ") +
                f"terkuat {best[0]} {best[1]:+.1f}% · terlemah {worst[0]} {worst[1]:+.1f}% (sumber: Binance klines 1h)")

    def daily_report(self) -> str:
        since, until = yesterday_start(), utc_day_start()
        s = self.j.summary(since)
        ev = self.j.events_since(since)
        n_sig = sum(e["kind"] == "signal" for e in ev)
        blocks = [e["message"] for e in ev if e["kind"] == "shield_block"]
        halts = [e["message"] for e in ev if e["kind"] == "halt"]
        top = sorted(s["by_symbol"].items(), key=lambda kv: kv[1])
        date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%d %b %Y")
        return "\n".join([
            f"📊 LAPORAN HARIAN {date} (UTC) · mode {self.cfg.mode.upper()}",
            self._market_line(since, until),
            f"Scout: {n_sig} sinyal dari {len(self.shield.passed_pairs())} pasangan aktif",
            f"Shield: memblokir {len(blocks)}" + (f" — contoh: {blocks[-1][:140]}" if blocks else ""),
            f"Pilot: {s['trades']} trade selesai · win {s['wins']} · limit tak terisi {s['unfilled']} · "
            f"P&L {s['pnl']:+.2f} USDT (fee {s['fees']:.2f})",
            (f"Terbaik {top[-1][0]} {top[-1][1]:+.2f} · terburuk {top[0][0]} {top[0][1]:+.2f}" if top else ""),
            *self._pos_lines(),
            ("Rem: " + " | ".join(h[:100] for h in halts)) if halts else "Rem: tidak terpicu",
            f"➡️ Keputusan untuk Anda: {self._decision_needed(s)}",
        ]).replace("\n\n", "\n")

    def _decision_needed(self, s: dict) -> str:
        if self.shield.halted():
            return "Bot sedang berhenti. Tinjau trade terakhir lalu ketik /resume, atau biarkan berhenti."
        if not self.shield.passed_pairs():
            return "Belum ada pasangan lulus Lab (atau hasil Lab > 30 hari). Jalankan Lab di server."
        if s["trades"] >= 10 and s["pnl"] < 0 and (s["profit_factor"] or 0) < 0.8:
            return "Hasil kemarin jauh di bawah Lab. Pertimbangkan /stop dan uji ulang Lab."
        return "Tidak ada. Cukup baca laporan ini."

    def weekly_review(self) -> str:
        s = self.j.summary(self.j.week_start())
        strat = "\n".join(f"  · {k}: {v:+.2f} USDT" for k, v in sorted(s["by_strategy"].items())) or "  · belum ada"
        sym = "\n".join(f"  · {k}: {v:+.2f} USDT" for k, v in sorted(s["by_symbol"].items())) or "  · belum ada"
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] else "–"
        return (f"🗓️ REVIEW MINGGUAN · mode {self.cfg.mode.upper()}\n"
                f"Trade {s['trades']} · win rate {s['win_rate']:.0f}% · profit factor {pf}\n"
                f"P&L {s['pnl']:+.2f} USDT setelah fee · total fee {s['fees']:.2f} · limit tak terisi {s['unfilled']}\n"
                f"Diblokir Shield: {s['blocked']}\nPer strategi:\n{strat}\nPer koin:\n{sym}\n"
                "Catatan: kurang dari ±30 trade = terlalu sedikit untuk menyimpulkan apa pun.")

    def lab_text(self) -> str:
        if not self.shield.lab:
            return "Lab belum pernah dijalankan. Lihat docs/DEPLOY.md langkah 7."
        ok = [v for v in self.shield.lab.values() if v.get("passed")]
        out = [f"Lab: {len(ok)} dari {len(self.shield.lab)} pasangan lulus (data asli, setelah fee)."]
        for v in sorted(ok, key=lambda v: -(v["out_of_sample"]["profit_factor"] or 0))[:15]:
            o = v["out_of_sample"]
            out.append(f"✅ {v['strategy']} {v['symbol']}: {o['trades']} trade OOS, PF {o['profit_factor']}, "
                       f"walk-forward {v['folds_positive']}, DD95 {v['mc_dd95_pct']}%")
        tested = datetime.fromtimestamp(max(v["tested_at"] for v in self.shield.lab.values()), tz=WIB)
        return "\n".join(out + [f"Diuji: {tested:%d %b %Y %H:%M} WIB · detail: data/lab_report.md"])
