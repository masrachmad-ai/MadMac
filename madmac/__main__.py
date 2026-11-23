"""Usage:
  python -m madmac run        start the bot (24/7)
  python -m madmac lab        backtest all strategies on real Binance data
  python -m madmac selfcheck  test keys, connection, stop-loss placement (testnet only)
"""
from __future__ import annotations

import sys
import time

from .config import load_config


def selfcheck() -> int:
    from .binance import Binance
    from .coordinator import Telegram
    cfg = load_config()
    if cfg.mode != "testnet":
        print("selfcheck hanya boleh di testnet. Ubah mode: testnet dulu.")
        return 1
    ex = Binance("testnet")
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

    step("Jam server Binance", lambda: ex.sync_time() or f"selisih {ex.time_offset_ms} ms")
    rules = step("Aturan simbol", lambda: ex.symbol_rules(cfg.symbol))
    step("Harga", lambda: ex.price(cfg.symbol)[0])
    step("Saldo testnet (USDT)", ex.equity)
    step("Posisi", lambda: ex.position(cfg.symbol))
    step("Margin type", lambda: ex.set_margin_type(cfg.symbol, cfg.margin_type))
    if rules and ok:
        price = ex.price(cfg.symbol)[0]
        tag = f"mmcheck{int(time.time())}"
        # A far-away stop with closePosition places a resting order without opening anything.
        step("Pasang stop-loss uji (jauh dari harga)",
             lambda: ex.protective_order(cfg.symbol, "SELL", "STOP_MARKET",
                                         round(price * 0.5 / rules.tick_size) * rules.tick_size, tag) and "terpasang")
        step("Stop terbaca kembali dari Binance",
             lambda: [o["type"] for o in ex.open_protective_orders(cfg.symbol)])
        step("Hapus stop uji", lambda: ex.cancel_all(cfg.symbol))
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
        cfg = load_config()
        res = lab.run(cfg)
        print(lab.report(res))
    elif cmd == "selfcheck":
        return selfcheck()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
