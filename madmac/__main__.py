"""Usage:
  python -m madmac run        start the bot (24/7)
  python -m madmac lab        backtest every strategy × coin on real Binance data
  python -m madmac selfcheck  test keys, connection, data feeds, order types (testnet only)
"""
from __future__ import annotations

import sys
import time

from .config import load_config


def selfcheck() -> int:
    from .binance import Binance
    from .coordinator import Telegram
    from .shield import floor_step
    cfg = load_config()
    if cfg.mode != "testnet":
        print("selfcheck hanya boleh di testnet. Ubah mode: testnet dulu.")
        return 1
    ex = Binance("testnet")
    mkt = Binance("live", key="", secret="")
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            r = fn()
            print(f"✅ {name}" + (f": {r}" if r is not None else ""))
            return r
        except Exception as e:
            ok = False
            print(f"❌ {name}: {e}")

    print("— Data pasar ASLI (sumber sinyal) —")
    step("Jam server", lambda: mkt.server_time())
    sym = cfg.symbols[0]
    step(f"Candle {sym} (1000)", lambda: len(mkt.klines(sym, cfg.timeframe, 1000)))
    step(f"Open interest {sym}", lambda: f"{len(mkt.open_interest_hist(sym, cfg.timeframe, 50))} titik")
    step(f"Funding {sym}", lambda: mkt.funding_now(sym)[1])

    print("— Testnet (order) —")
    step("Jam server", lambda: ex.sync_time() or f"selisih {ex.time_offset_ms} ms")
    rules = step("Aturan semua koin", lambda: ex.symbol_rules_all(cfg.symbols))
    if rules is not None:
        missing = [s for s in cfg.symbols if s not in rules]
        if missing:
            print(f"⚠️ Tidak tersedia di testnet: {missing} (koin ini akan dilewati)")
    step("Saldo testnet (USDT)", ex.equity)
    step("Posisi", ex.positions)
    step("Margin type", lambda: ex.set_margin_type(sym, cfg.margin_type))
    if rules and sym in rules and ok:
        r = rules[sym]
        price = ex.price(sym)[0]
        tag = f"mmcheck{int(time.time())}"
        # Far-away orders: they rest in the book without ever filling.
        step("Pasang stop-loss uji (jauh dari harga)",
             lambda: ex.protective_order(sym, "SELL", "STOP_MARKET", floor_step(price * 0.5, r.tick_size), tag)
             and "terpasang")
        step("Stop terbaca kembali dari Binance", lambda: [o["type"] for o in ex.open_protective_orders(sym)])
        qty = floor_step(max(r.min_notional * 1.5 / (price * 0.5), r.min_qty), r.step_size)
        step("Limit post-only uji (jauh dari harga)",
             lambda: ex.limit_order(sym, "BUY", qty, floor_step(price * 0.5, r.tick_size), tag + "L")["status"])
        step("Batalkan limit uji", lambda: ex.cancel_order(sym, tag + "L"))
        step("Hapus stop uji", lambda: ex.cancel_all(sym))
    tg = Telegram()
    if tg.enabled:
        step("Telegram", lambda: tg.send("✅ selfcheck MadMac: Telegram tersambung."))
    else:
        print("⚠️ Telegram belum diatur (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        ok = False
    print("\nSEMUA OK" if ok else "\nADA YANG GAGAL — jangan jalankan bot sebelum diperbaiki.")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        from .bot import Bot
        Bot(load_config()).run_forever()
    elif cmd == "lab":
        from . import lab
        res = lab.run(load_config())
        print(lab.report(res))
    elif cmd == "selfcheck":
        return selfcheck()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
