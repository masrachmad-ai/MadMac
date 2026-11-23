"""STRESS TESTS — the 7 common failures from the brief, plus permission and
brake tests. Each test name says what failure it simulates."""
from __future__ import annotations

import os
import time
from dataclasses import replace

import pytest
import requests

from madmac.binance import Binance
from madmac.config import Config, LabRules
from madmac.coordinator import Coordinator, Telegram
from madmac.indicators import Candle
from madmac.journal import Journal, utc_day_start
from madmac.pilot import Pilot, trade_id
from madmac.scout import StaleFeed, check_fresh, closed_only
from madmac.shield import MarketSnapshot, Shield
from madmac.strategies import Signal
from tests.fakes import RULES, FakeExchange, Sink, TimeoutAwareExchange

NOW = time.time()
LAB_OK = {"ema_pullback": {"passed": True, "tested_at": NOW, "symbol": "BTCUSDT", "timeframe": "5m"}}


def cfg(**kw) -> Config:
    base = Config(risk_per_trade_pct=2.0, max_leverage=20, strategies=["ema_pullback"], lab=LabRules())
    return replace(base, **kw).validate()


def sig(stop=89_550.0, target=90_675.0, side="LONG", t=None, entry=90_000.0) -> Signal:
    return Signal("ema_pullback", side, entry, stop, target, "uji", t or int(NOW * 1000))


@pytest.fixture
def env(tmp_path):
    c = cfg()
    j = Journal(tmp_path / "j.db")
    sh = Shield(c, j, LAB_OK)
    ex = FakeExchange()
    sink = Sink()
    pilot = Pilot(c, ex, j, sh, sink)
    return c, j, sh, ex, pilot, sink


def snap(ex, **kw):
    m = MarketSnapshot(ex.equity(), ex._price, ex.price_ts, ex.pos["qty"], RULES)
    return replace(m, **kw)


# ---------------------------------------------------------------- 1. duplicate orders
def test_duplicate_order_same_signal_executes_once(env):
    c, j, sh, ex, pilot, sink = env
    s = sig()
    d = sh.check(s, snap(ex))
    assert d.approved
    pilot.execute(s, d, "telegram:1", 0.1)
    # user double-taps ✅, or the bot restarts and sees the same signal again
    assert pilot.execute(s, d, "telegram:1", 0.1) is None
    assert ex.market_calls == 1
    # and Shield refuses a second position anyway
    assert not sh.check(sig(t=s.candle_time + 300_000), snap(ex)).approved


# ---------------------------------------------------------------- 2. missed stop
def test_stop_rejected_closes_position_and_halts(env):
    c, j, sh, ex, pilot, sink = env
    ex.reject_protective = True
    s = sig()
    pilot.execute(s, sh.check(s, snap(ex)), "telegram:1", 0.1)
    assert ex.pos["qty"] == 0, "posisi tanpa stop harus ditutup"
    assert sh.halted()
    assert any("DITUTUP" in m for m in sink.msgs)


def test_stop_rejected_without_close_permission_alarms_only(env, tmp_path):
    c, j, sh, ex, pilot, sink = env
    pilot.cfg = replace(c, close_if_stop_fails=False)
    ex.reject_protective = True
    s = sig()
    pilot.execute(s, sh.check(s, snap(ex)), "telegram:1", 0.1)
    assert ex.pos["qty"] != 0, "tanpa izin, bot tidak boleh menutup"
    assert sh.halted() and any("TANPA STOP" in m for m in sink.msgs)


def test_stop_disappears_later_is_replaced(env):
    c, j, sh, ex, pilot, sink = env
    s = sig()
    pilot.execute(s, sh.check(s, snap(ex)), "telegram:1", 0.1)
    ex.protective.clear()                     # someone cancels it in the app
    pilot.reconcile()
    assert any(o["type"] == "STOP_MARKET" for o in ex.protective)


# ---------------------------------------------------------------- 3. wrong position size
def test_size_matches_risk_and_respects_exchange_steps(env):
    c, j, sh, ex, pilot, sink = env
    d = sh.check(sig(), snap(ex))
    assert d.approved
    assert d.risk_usdt <= 1000 * 0.02 + 1e-9          # never more than 2%
    assert abs(d.qty / 0.001 - round(d.qty / 0.001)) < 1e-9   # step size 0.001
    assert d.leverage <= 20
    assert d.notional_usdt / 900 <= d.leverage        # margin fits


def test_leverage_cap_shrinks_size_never_raises_cap(env):
    c, j, sh, ex, pilot, sink = env
    sh.cfg = replace(c, max_leverage=2, max_fee_share_of_risk_pct=100)
    d = sh.check(sig(), snap(ex))
    assert d.approved and d.leverage <= 2 and d.risk_usdt < 20


def test_tight_scalping_stop_blocked_by_fee_guard(env):
    """Your 2% / 20x choice with a 0.1% stop: fees eat ~40% of the risk."""
    c, j, sh, ex, pilot, sink = env
    d = sh.check(sig(stop=89_910.0, target=90_135.0), snap(ex))
    assert not d.approved and any("fee" in r for r in d.reasons)


def test_account_too_small_for_min_notional(env):
    c, j, sh, ex, pilot, sink = env
    d = sh.check(sig(), snap(ex, equity_usdt=20.0))
    assert not d.approved and any("terlalu kecil" in r for r in d.reasons)


# ---------------------------------------------------------------- 4. stale price feed
def test_stale_price_blocks_entry(env):
    c, j, sh, ex, pilot, sink = env
    d = sh.check(sig(), snap(ex, price_ts=time.time() - 120))
    assert not d.approved and any("basi" in r for r in d.reasons)


def test_stale_candles_raise():
    now = int(time.time() * 1000)
    old = [Candle(0, 1, 1, 1, 1, 1, now - 3_600_000)]
    with pytest.raises(StaleFeed):
        check_fresh(old, now, 300, 30)
    # the still-forming candle is never used
    forming = Candle(now - 1000, 1, 1, 1, 1, 1, now + 299_000)
    assert closed_only([forming], now) == []


# ---------------------------------------------------------------- 5. API timeout
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, str(body)

    def json(self):
        return self._b


class TimeoutThenFoundSession:
    """POST /order times out, but the order DID reach Binance."""

    def __init__(self):
        self.posts = 0

    def request(self, method, url, headers=None, timeout=None):
        if method == "POST":
            self.posts += 1
            raise requests.Timeout("read timed out")
        return _Resp(200, {"clientOrderId": "x", "executedQty": "0.01", "avgPrice": "90000", "status": "FILLED"})


def test_api_timeout_no_duplicate_real_client(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    sess = TimeoutThenFoundSession()
    ex = Binance("testnet", session=sess, key="k", secret="s")
    o = ex.market_order("BTCUSDT", "BUY", 0.01, "mmtest")
    assert sess.posts == 1, "order tidak boleh dikirim ulang setelah timeout"
    assert o["status"] == "FILLED"


def test_api_timeout_unknown_halts(env):
    c, j, sh, ex, pilot, sink = env
    ex.timeout_on_order, ex.order_lands_anyway = True, False
    s = sig()
    assert pilot.execute(s, sh.check(s, snap(ex)), "telegram:1", 0.1) is None
    assert sh.halted()


def test_api_timeout_order_landed_is_recovered(tmp_path):
    c = cfg()
    j = Journal(tmp_path / "j.db")
    sh = Shield(c, j, LAB_OK)
    ex = TimeoutAwareExchange()
    ex.timeout_on_order = True
    p = Pilot(c, ex, j, sh, Sink())
    s = sig()
    assert p.execute(s, sh.check(s, snap(ex)), "telegram:1", 0.1)
    assert ex.market_calls == 1 and len(j.open_trades()) == 1


# ---------------------------------------------------------------- 6. timezone error
def test_day_boundary_is_utc_whatever_server_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Jakarta")
    time.tzset()
    try:
        ts = 1_767_225_600 + 3600 * 5      # 2026-01-01 05:00 UTC = 12:00 WIB
        assert utc_day_start(ts) == 1_767_225_600
        ts2 = 1_767_225_600 - 60            # 2025-12-31 23:59 UTC = 06:59 WIB next day
        assert utc_day_start(ts2) == 1_767_225_600 - 86400
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_daily_limit_resets_at_utc_midnight(env):
    c, j, sh, ex, pilot, sink = env
    day = utc_day_start()
    sh.update_day(1000, now=day + 10)
    sh.update_day(960, now=day + 20)           # −4% → halt until next day
    assert sh.halted(now=day + 30)
    assert sh.halted(now=day + 86_400 + 1) is None


# ---------------------------------------------------------------- 7. partial fill
def test_partial_fill_protects_actual_quantity(env):
    c, j, sh, ex, pilot, sink = env
    ex.fill_ratio = 0.5
    s = sig()
    d = sh.check(s, snap(ex))
    pilot.execute(s, d, "telegram:1", 0.1)
    t = j.open_trades()[0]
    assert t["qty"] == pytest.approx(round(d.qty * 0.5, 3))
    assert any(o["type"] == "STOP_MARKET" for o in ex.protective)
    assert j.events_since(0, ("partial_fill",))


# ---------------------------------------------------------------- permissions & brakes
def test_pilot_refuses_without_shield_or_human(env):
    c, j, sh, ex, pilot, sink = env
    s = sig()
    d = sh.check(s, snap(ex))
    with pytest.raises(PermissionError):
        pilot.execute(s, d, "", 0.1)
    d.approved = False
    with pytest.raises(PermissionError):
        pilot.execute(s, d, "telegram:1", 0.1)
    assert ex.market_calls == 0


def test_strategy_not_passed_by_lab_is_blocked(env):
    c, j, sh, ex, pilot, sink = env
    sh.lab = {}
    assert not sh.check(sig(), snap(ex)).approved


def test_live_needs_go_live(env):
    c, j, sh, ex, pilot, sink = env
    sh.cfg = replace(c, mode="live")
    assert not sh.check(sig(), snap(ex)).approved
    j.set("go_live_confirmed", {"by": "telegram:1"})
    assert sh.check(sig(), snap(ex)).approved


def test_three_losses_halt_until_resume(env):
    c, j, sh, ex, pilot, sink = env
    for i in range(3):
        j.open_trade(f"t{i}", "testnet", "ema_pullback", "LONG", 0.01, 1, 0.5, 2, "x")
        j.close_trade(f"t{i}", 0.5, -1.0, 0.1, "rugi")
    sh.update_day(1000)
    assert sh.halted() and "/resume" in sh.halted()
    assert not sh.check(sig(), snap(ex)).approved
    sh.resume("telegram:1")
    assert sh.halted() is None


def test_closed_trade_is_journaled_from_exchange(env):
    c, j, sh, ex, pilot, sink = env
    s = sig()
    pilot.execute(s, sh.check(s, snap(ex)), "telegram:1", 0.1)
    ex.hit_stop(89_550, pnl=-15.0, fee=1.5)
    pilot.reconcile()
    t = j.closed_since(0)[0]
    assert t["pnl_usdt"] == pytest.approx(-16.5)
    assert j.consecutive_losses() == 1


class _Bot:
    def __init__(self, c, sh, pilot):
        self.cfg, self.shield, self.pilot = c, sh, pilot
        self.approved = []

    def on_approved(self, s, approved_by):
        self.approved.append(approved_by)


def test_stranger_cannot_approve_or_command(env):
    c, j, sh, ex, pilot, sink = env
    tg = Telegram(token="", chat_id="111")
    tg.send = Sink()
    bot = _Bot(c, sh, pilot)
    co = Coordinator(bot, tg, j)
    s = sig()
    co.ask_approval("sid", s, sh.check(s, snap(ex)))
    co.handle({"callback_query": {"id": "q", "data": "ok:sid", "message": {"chat": {"id": 999}}}})
    co.handle({"message": {"chat": {"id": 999}, "text": "/resume"}})
    co.handle({"message": {"chat": {"id": 999}, "text": "go live"}})
    assert bot.approved == [] and not j.get("go_live_confirmed")


def test_stop_command_is_instant_and_kills_pending(env):
    c, j, sh, ex, pilot, sink = env
    tg = Telegram(token="", chat_id="111")
    tg.send = Sink()
    co = Coordinator(_Bot(c, sh, pilot), tg, j)
    s = sig()
    co.ask_approval("sid", s, sh.check(s, snap(ex)))
    t0 = time.time()
    co.handle({"message": {"chat": {"id": 111}, "text": "/stop"}})
    assert time.time() - t0 < 1 and sh.halted() and co.pending == {}


def test_expired_signal_never_executes(env):
    c, j, sh, ex, pilot, sink = env
    tg = Telegram(token="", chat_id="111")
    tg.send = Sink()
    bot = _Bot(c, sh, pilot)
    co = Coordinator(bot, tg, j)
    s = sig()
    co.ask_approval("sid", s, sh.check(s, snap(ex)))
    co.pending["sid"]["expires"] = 0
    co.expire()
    co.handle({"callback_query": {"id": "q", "data": "ok:sid", "message": {"chat": {"id": 111}}}})
    assert bot.approved == []


# ---------------------------------------------------------------- end-to-end
def test_full_flow_signal_needs_your_tap(tmp_path):
    from madmac.bot import Bot
    c = cfg()
    ex = FakeExchange()
    tg = Telegram(token="", chat_id="111")
    sent = Sink()
    tg.send = sent
    bot = Bot(c, ex=ex, journal=Journal(tmp_path / "j.db"), tg=tg)
    bot.shield.lab = LAB_OK
    bot.rules = RULES
    bot.pilot.notify = sent
    s = sig()
    bot.on_signal(s)
    assert ex.market_calls == 0, "tidak ada order sebelum Anda menekan ✅"
    assert any("SINYAL LONG" in m for m in sent.msgs)
    sid = trade_id(s)
    bot.coord.handle({"callback_query": {"id": "q", "data": f"ok:{sid}", "message": {"chat": {"id": 111}}}})
    assert ex.market_calls == 1 and ex.pos["qty"] > 0
    assert any(o["type"] == "STOP_MARKET" for o in ex.protective)
    assert "LAPORAN" not in bot.status_text()


def test_daily_report_renders_without_market_data(tmp_path):
    from madmac.bot import Bot
    tg = Telegram(token="", chat_id="111")
    tg.send = Sink()
    bot = Bot(cfg(), ex=FakeExchange(), journal=Journal(tmp_path / "j.db"), tg=tg)
    r = bot.daily_report()
    assert "LAPORAN HARIAN" in r and "Keputusan untuk Anda" in r
    assert "REVIEW MINGGUAN" in bot.weekly_review()
