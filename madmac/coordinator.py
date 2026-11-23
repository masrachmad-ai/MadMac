"""COORDINATOR — the only agent you talk to (via Telegram).

One job: route your commands, ask for your approval, and send one daily
report. It NEVER opens, changes or closes a trade on its own: it only
forwards your explicit decision to Pilot.

Only messages from YOUR chat id (env TELEGRAM_CHAT_ID) are obeyed;
everyone else is ignored and logged.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from .journal import Journal, utc_day_start

WIB = timezone(timedelta(hours=7))

HELP = """Perintah:
/status — kondisi bot, posisi & P&L
/report — laporan harian sekarang
/week — review mingguan
/lab — hasil uji Lab terakhir
/stop — REM DARURAT: blokir semua entry baru
/resume — lepas rem (setelah Anda meninjau)
/flat — tutup SEMUA posisi & batalkan limit order (minta konfirmasi)
Ketik "go live" — hanya berlaku jika mode: live di config."""


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None, session=None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", ""))
        self.s = session or requests.Session()
        self.offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, **payload):
        if not self.enabled:
            print(f"[telegram off] {method}: {payload.get('text', '')}")
            return {}
        r = self.s.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=35)
        return r.json()

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        payload = {"chat_id": self.chat_id, "text": text[:4000]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
        try:
            self._call("sendMessage", **payload)
        except requests.RequestException as e:
            print(f"[telegram error] {e}")

    def updates(self) -> list[dict]:
        if not self.enabled:
            time.sleep(5)
            return []
        try:
            r = self._call("getUpdates", offset=self.offset, timeout=25)
        except requests.RequestException:
            time.sleep(5)
            return []
        res = r.get("result", [])
        if res:
            self.offset = res[-1]["update_id"] + 1
        return res


class Coordinator:
    def __init__(self, bot, tg: Telegram, journal: Journal):
        self.bot, self.tg, self.j = bot, tg, journal
        self.pending: dict[str, dict] = {}   # signal id → {sig, expires}
        self.confirm_flat_until = 0.0
        self.lock = threading.Lock()

    # -------------------------------------------------- approvals
    def ask_approval(self, sid: str, sig, decision) -> None:
        timeout = self.bot.cfg.approval_timeout_seconds
        with self.lock:
            self.pending[sid] = {"sig": sig, "expires": time.time() + timeout}
        self.tg.send(
            f"🔔 SINYAL {sig.symbol} {sig.side} ({sig.strategy})\n"
            f"Harga {sig.entry:,.6g} · Stop {sig.stop:,.6g} · Target {sig.target:,.6g}\n"
            f"Alasan: {sig.reason}\n{decision.text()}\n"
            f"Mode: {self.bot.cfg.mode.upper()} · berlaku {timeout} detik.",
            buttons=[("✅ Setujui", f"ok:{sid}"), ("❌ Tolak", f"no:{sid}")])

    def expire(self) -> None:
        now = time.time()
        with self.lock:
            for sid in [k for k, v in self.pending.items() if v["expires"] < now]:
                self.pending.pop(sid)
                self.j.log("coordinator", "expired", f"Sinyal {sid} kedaluwarsa tanpa jawaban — tidak dieksekusi.")
                self.tg.send("⌛ Sinyal kedaluwarsa, tidak ada order.")

    # -------------------------------------------------- inbound
    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            q = upd["callback_query"]
            chat = str(q.get("message", {}).get("chat", {}).get("id", ""))
            if chat != self.tg.chat_id:
                self.j.log("coordinator", "unauthorized", f"Klik tombol dari chat asing {chat} diabaikan.")
                return
            self.tg._call("answerCallbackQuery", callback_query_id=q["id"])
            action, _, sid = q.get("data", "").partition(":")
            with self.lock:
                item = self.pending.pop(sid, None)
            if not item:
                self.tg.send("Sinyal itu sudah kedaluwarsa atau sudah dijawab.")
                return
            if action == "ok":
                self.j.log("coordinator", "approved", f"Anda menyetujui {sid}.")
                self.bot.on_approved(item["sig"], approved_by=f"telegram:{chat}")
            else:
                self.j.log("coordinator", "rejected", f"Anda menolak {sid}.")
                self.tg.send("👍 Ditolak. Tidak ada order.")
            return

        msg = upd.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat != self.tg.chat_id:
            self.j.log("coordinator", "unauthorized", f"Pesan dari chat asing {chat} diabaikan.")
            return
        self.command(text, who=f"telegram:{chat}")

    def command(self, text: str, who: str) -> None:
        t = text.lower().split("@")[0]
        b = self.bot
        if t == "/stop":
            b.shield.halt(f"Rem darurat oleh {who}")
            with self.lock:
                self.pending.clear()
            self.tg.send("🛑 REM AKTIF. Tidak ada entry baru. Posisi terbuka tetap dilindungi stop di Binance.\n"
                         "Limit order yang belum terisi dibatalkan dalam 1 candle.\n"
                         "Ketik /flat untuk menutup posisi, /resume untuk lanjut.")
        elif t == "/resume":
            b.shield.resume(who)
            self.tg.send("▶️ Rem dilepas. Bot kembali memantau.")
        elif t == "/flat":
            self.confirm_flat_until = time.time() + 60
            self.tg.send("Yakin tutup SEMUA posisi sekarang di harga pasar? Ketik /flatyakin dalam 60 detik.")
        elif t == "/flatyakin":
            if time.time() > self.confirm_flat_until:
                self.tg.send("Konfirmasi kedaluwarsa. Ketik /flat lagi.")
                return
            self.confirm_flat_until = 0
            self.tg.send(b.pilot.flatten(who))
        elif t == "go live":
            if b.cfg.mode != "live":
                self.tg.send("Config masih mode: testnet. Ubah dulu ke mode: live di config.yaml, restart, lalu ketik go live.")
                return
            self.j.set("go_live_confirmed", {"by": who, "ts": int(time.time())})
            self.j.log("coordinator", "go_live", f"UANG ASLI diaktifkan oleh {who}")
            self.tg.send("⚠️ GO LIVE dikonfirmasi. Trading memakai UANG ASLI mulai sinyal berikutnya.")
        elif t == "/status":
            self.tg.send(b.status_text())
        elif t == "/report":
            self.tg.send(b.daily_report())
        elif t == "/week":
            self.tg.send(b.weekly_review())
        elif t == "/lab":
            self.tg.send(b.lab_text())
        else:
            self.tg.send(HELP)

    def poll_forever(self) -> None:
        while True:
            for u in self.tg.updates():
                try:
                    self.handle(u)
                except Exception as e:  # a bad message must never kill the bot
                    self.j.log("coordinator", "error", f"{type(e).__name__}: {e}")

    # -------------------------------------------------- schedule
    def due_daily(self, now: float | None = None) -> bool:
        n = datetime.fromtimestamp(now or time.time(), tz=WIB)
        key = n.strftime("%Y-%m-%d")
        if n.hour >= self.bot.cfg.daily_report_hour_wib and self.j.get("last_daily") != key:
            self.j.set("last_daily", key)
            return True
        return False

    def due_weekly(self, now: float | None = None) -> bool:
        n = datetime.fromtimestamp(now or time.time(), tz=WIB)
        key = n.strftime("%G-W%V")
        if (n.strftime("%A").lower() == self.bot.cfg.weekly_review_day
                and n.hour >= self.bot.cfg.daily_report_hour_wib and self.j.get("last_weekly") != key):
            self.j.set("last_weekly", key)
            return True
        return False


def yesterday_start() -> int:
    return utc_day_start() - 86400
