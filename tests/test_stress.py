"""STRESS TESTS — the 7 common failures from the brief, the multi-coin /
maker-order risks, and permission & brake tests. Each test name says what
failure it simulates."""
from __future__ import annotations

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
from madmac.shield import MarketSnapshot, Shield, lab_key
from madmac.strategies import Signal
from tests.fakes import PRICES, RULES, FakeExchange, Sink, TimeoutAwareExchange

NOW = time.time()
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
STRATS = ["smc_sweep", "ema_pullback"]
LAB_OK = {lab_key(s, y): {"strategy": s, "symbol": y, "timeframe": "5m", "passed": True, "tested_at": NOW}
          for s in STRATS for y in SYMS}


def cfg(**kw) -> Config:
    base = Config(symbols=SYMS, strategies=STRATS, approval="auto", entry_order="maker", lab=LabRules())
    return replace(base, **kw).validate()


def sig(sym="BTCUSDT", side="LONG", stop_pct=0.5, rr=1.5, t=None, strategy="smc_sweep", hold=36) -> Signal:
    e = PRICES[sym]
    d = e * stop_pct / 100
    stop, tgt = (e - d, e + rr * d) if side == "LONG" else (e + d, e - rr * d)
    return Signal(strategy, side, e, stop, tgt, "uji", t or int(NOW * 1000), sym, hold)


class Env:
    def __init__(self, tmp_path, **kw):
        self.c = cfg(**kw)
        self.j = Journal(tmp_path / "j.db")
        self.sh = Shield(self.c, self.j, LAB_OK)
        self.ex = FakeExchange()
        self.alarms, self.infos = Sink(), Sink()
        self.p = Pilot(self.c, self.ex, self.j, self.sh, self.alarms, self.infos)
        self.p.rules = dict(RULES)

    def snap(self, sym="BTCUSDT", **kw):
        m = MarketSnapshot(self.ex.equity(), self.ex.px[sym], self.ex.price_ts,
                           self.ex.position(sym)["qty"], RULES[sym])
        return replace(m, **kw)

    def go(self, s, fill=True):
        """Shield → Pilot → (market trades through limit) → reconcile."""
        d = self.sh.check(s, self.snap(s.symbol))
        assert d.approved, d.reasons
        tid = self.p.execute(s, d, "config:approval=auto")
        if fill and tid and self.c.entry_order == "maker":
            self.ex.fill(tid)
            self.p.reconcile()
        return tid, d


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def has(ex, sym, typ):
    return any(o["type"] == typ for o in ex.open_protective_orders(sym))


# ================================================================ 1. duplicate orders
def test_duplicate_order_same_signal_executes_once(env):
    s = sig()
    tid, d = env.go(s)
    assert env.p.execute(s, d, "config:approval=auto") is None
    assert env.ex.limit_calls == 1
    # a NEW signal on the same coin is refused while a position exists there
    assert not env.sh.check(sig(t=s.candle_time + 300_000), env.snap()).approved


# ================================================================ 2. missed stop
def test_stop_rejected_closes_position_and_halts(env):
    env.ex.reject_protective = True
    env.go(sig())
    assert env.ex.position("BTCUSDT")["qty"] == 0, "posisi tanpa stop harus ditutup"
    assert env.sh.halted() and any("DITUTUP" in m for m in env.alarms.msgs)


def test_stop_rejected_without_close_permission_alarms_only(tmp_path):
    e = Env(tmp_path, close_if_stop_fails=False)
    e.ex.reject_protective = True
    e.go(sig())
    assert e.ex.position("BTCUSDT")["qty"] != 0
    assert e.sh.halted() and any("TANPA STOP" in m for m in e.alarms.msgs)


def test_stop_disappears_later_is_replaced(env):
    env.go(sig())
    env.ex.prot["BTCUSDT"] = [o for o in env.ex.prot["BTCUSDT"] if o["type"] != "STOP_MARKET"]
    env.p.reconcile()
    assert has(env.ex, "BTCUSDT", "STOP_MARKET")


# ================================================================ 3. wrong position size
def test_size_matches_risk_and_exchange_steps(env):
    d = env.sh.check(sig(), env.snap())
    assert d.approved
    assert d.risk_usdt <= 10_000 * 0.005 + 1e-9
    assert abs(d.qty / 0.001 - round(d.qty / 0.001)) < 1e-9
    assert d.leverage <= 10


def test_leverage_cap_shrinks_size_never_raises_cap(tmp_path):
    e = Env(tmp_path, max_leverage=1, max_fee_share_of_risk_pct=100)
    d = e.sh.check(sig(stop_pct=0.3), e.snap())
    assert d.approved and d.leverage <= 1 and d.risk_usdt < 50


def test_tight_scalping_stop_blocked_by_fee_guard(env):
    """A 1-minute-style 0.1% stop: fees eat > 25% of the risk."""
    d = env.sh.check(sig(stop_pct=0.1), env.snap())
    assert not d.approved and any("fee" in r for r in d.reasons)


def test_account_too_small_for_min_notional(env):
    d = env.sh.check(sig(), env.snap(equity_usdt=50.0))
    assert not d.approved and any("terlalu kecil" in r for r in d.reasons)


# ================================================================ 3b. portfolio risk (many coins)
def test_max_three_positions(env):
    env.go(sig("BTCUSDT"))
    env.go(sig("ETHUSDT", side="SHORT"))
    env.go(sig("SOLUSDT"))
    d = env.sh.check(sig("XRPUSDT", side="SHORT"), env.snap("XRPUSDT"))
    assert not d.approved and any("maks 3" in r for r in d.reasons)


def test_same_direction_cap_blocks_third_long(env):
    env.go(sig("BTCUSDT"))
    env.go(sig("ETHUSDT"))
    d = env.sh.check(sig("SOLUSDT"), env.snap("SOLUSDT"))
    assert not d.approved and any("searah" in r for r in d.reasons)


def test_total_open_risk_cap_shrinks_then_blocks(tmp_path):
    e = Env(tmp_path, max_total_open_risk_pct=0.7, max_same_direction=3)
    e.go(sig("BTCUSDT"))                    # uses ≈0.42% (price risk)
    d = e.sh.check(sig("ETHUSDT"), e.snap("ETHUSDT"))
    assert d.approved and d.risk_usdt < 30 and "batas total risiko" in d.reasons[0]   # shrunk
    e2 = Env(tmp_path / "b", max_total_open_risk_pct=0.6, max_same_direction=3)
    e2.go(sig("BTCUSDT"))
    d2 = e2.sh.check(sig("ETHUSDT"), e2.snap("ETHUSDT"))
    assert not d2.approved and any("total risiko" in r for r in d2.reasons)        # no room


def test_pending_limit_orders_count_as_exposure(env):
    env.go(sig("BTCUSDT"), fill=False)      # not filled yet
    env.go(sig("ETHUSDT"), fill=False)
    d = env.sh.check(sig("SOLUSDT"), env.snap("SOLUSDT"))
    assert not d.approved


# ================================================================ 4. stale price feed
def test_stale_price_blocks_entry(env):
    d = env.sh.check(sig(), env.snap(price_ts=time.time() - 120))
    assert not d.approved and any("basi" in r for r in d.reasons)


def test_stale_candles_raise_and_forming_candle_is_ignored():
    now = int(time.time() * 1000)
    with pytest.raises(StaleFeed):
        check_fresh([Candle(0, 1, 1, 1, 1, 1, now - 3_600_000)], now, 300, 30)
    forming = Candle(now - 1000, 1, 1, 1, 1, 1, now + 299_000)
    assert closed_only([forming], now) == []


# ================================================================ 5. API timeout
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, str(body)

    def json(self):
        return self._b


class TimeoutThenFoundSession:
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
    o2 = ex.limit_order("BTCUSDT", "BUY", 0.01, 89000.0, "mmtest2")
    assert sess.posts == 2 and o2["status"] == "FILLED"


def test_float_noise_never_reaches_binance():
    from madmac.binance import _num
    assert _num(0.1 + 0.2) == "0.3" and _num(89550.0) == "89550"


def test_api_timeout_unknown_halts(tmp_path):
    e = Env(tmp_path, entry_order="taker")
    e.ex.timeout_on_order, e.ex.order_lands_anyway = True, False
    s = sig()
    assert e.p.execute(s, e.sh.check(s, e.snap()), "config:approval=auto") is None
    assert e.sh.halted()


def test_api_timeout_order_landed_is_recovered(tmp_path):
    e = Env(tmp_path, entry_order="taker")
    e.ex = TimeoutAwareExchange()
    e.p.ex = e.ex
    e.ex.timeout_on_order = True
    s = sig()
    assert e.p.execute(s, e.sh.check(s, e.snap()), "config:approval=auto")
    assert e.ex.market_calls == 1 and len(e.j.open_trades()) == 1


# ================================================================ 6. timezone
def test_day_boundary_is_utc_whatever_server_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Jakarta")
    time.tzset()
    try:
        ts = 1_767_225_600 + 3600 * 5      # 2026-01-01 05:00 UTC = 12:00 WIB
        assert utc_day_start(ts) == 1_767_225_600
        assert utc_day_start(1_767_225_600 - 60) == 1_767_225_600 - 86400
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_daily_limit_resets_at_utc_midnight(env):
    day = utc_day_start()
    env.sh.update_day(10_000, now=day + 10)
    env.sh.update_day(9_600, now=day + 20)     # −4%
    assert env.sh.halted(now=day + 30)
    assert env.sh.halted(now=day + 86_400 + 1) is None


# ================================================================ 7. partial fill
def test_partial_fill_gets_stop_immediately_and_tp_for_filled_qty(env):
    s = sig()
    d = env.sh.check(s, env.snap())
    tid = env.p.execute(s, d, "config:approval=auto")
    env.ex.fill(tid, ratio=0.5)
    env.p.reconcile()                             # still working → stop only
    assert has(env.ex, "BTCUSDT", "STOP_MARKET") and not has(env.ex, "BTCUSDT", "TP_LIMIT")
    env.p.reconcile(now=time.time() + 400)        # timeout → cancel rest, TP for filled
    t = env.j.trade(tid)
    assert t["status"] == "open" and t["qty"] == pytest.approx(round(d.qty * 0.5, 8))
    assert has(env.ex, "BTCUSDT", "TP_LIMIT")
    assert env.ex.orders[tid]["status"] == "CANCELED"


# ================================================================ maker order life-cycle
def test_unfilled_limit_is_cancelled_after_timeout(env):
    tid, _ = env.go(sig(), fill=False)
    env.p.reconcile()
    assert env.j.trade(tid)["status"] == "pending"
    env.p.reconcile(now=time.time() + 400)
    assert env.j.trade(tid)["status"] == "cancelled"
    assert env.ex.position("BTCUSDT")["qty"] == 0


def test_post_only_rejected_means_no_trade(env):
    env.ex.gtx_reject = True
    s = sig()
    tid = env.p.execute(s, env.sh.check(s, env.snap()), "config:approval=auto")
    assert env.j.trade(tid)["status"] == "cancelled" and env.ex.market_calls == 0


def test_time_stop_closes_after_max_hold(env):
    tid, _ = env.go(sig(hold=2))
    env.p.reconcile(now=time.time() + 3 * 300 + 5)
    assert env.ex.position("BTCUSDT")["qty"] == 0
    assert env.j.events_since(0, ("time_exit",))


def test_closed_trade_is_journaled_from_exchange_fills(env):
    tid, _ = env.go(sig())
    env.ex.hit_stop("BTCUSDT", 89_550, pnl=-45.0, fee=3.0)
    env.p.reconcile()
    t = env.j.closed_since(0)[0]
    assert t["pnl_usdt"] == pytest.approx(-48.0) and env.j.consecutive_losses() == 1


def test_stranger_position_halts_and_is_not_touched(env):
    env.ex.pos["ETHUSDT"] = {"qty": 1.0, "entry": 3000.0}
    env.p.reconcile()
    assert env.sh.halted() and env.ex.position("ETHUSDT")["qty"] == 1.0


# ================================================================ permissions & brakes
def test_pilot_refuses_without_shield_or_approval(env):
    s = sig()
    d = env.sh.check(s, env.snap())
    with pytest.raises(PermissionError):
        env.p.execute(s, d, "")
    d.approved = False
    with pytest.raises(PermissionError):
        env.p.execute(s, d, "config:approval=auto")
    assert env.ex.limit_calls == 0 and env.ex.market_calls == 0


def test_pair_not_passed_by_lab_is_blocked(env):
    env.sh.lab = {k: v for k, v in LAB_OK.items() if k != lab_key("smc_sweep", "BTCUSDT")}
    assert not env.sh.check(sig(), env.snap()).approved
    assert env.sh.check(sig(strategy="ema_pullback"), env.snap()).approved


def test_old_lab_result_is_blocked(env):
    env.sh.lab = {k: {**v, "tested_at": NOW - 40 * 86400} for k, v in LAB_OK.items()}
    assert not env.sh.check(sig(), env.snap()).approved


def test_live_needs_go_live(tmp_path):
    e = Env(tmp_path, mode="live")
    assert not e.sh.check(sig(), e.snap()).approved
    e.j.set("go_live_confirmed", {"by": "telegram:1"})
    assert e.sh.check(sig(), e.snap()).approved


def test_three_losses_halt_until_resume(env):
    for i in range(3):
        env.j.add_trade(f"t{i}", "BTCUSDT", "testnet", "smc_sweep", "LONG", 0.01, 1, 0.5, 2, "x", 0, "open")
        env.j.close_trade(f"t{i}", 0.5, -1.0, 0.1, "rugi")
    env.sh.update_day(10_000)
    assert env.sh.halted() and "/resume" in env.sh.halted()
    assert not env.sh.check(sig(), env.snap()).approved
    env.sh.resume("telegram:1")
    assert env.sh.halted() is None


class _Bot:
    def __init__(self, c, sh, pilot):
        self.cfg, self.shield, self.pilot = c, sh, pilot
        self.approved = []

    def on_approved(self, s, approved_by):
        self.approved.append(approved_by)


def _coord(env):
    tg = Telegram(token="", chat_id="111")
    tg.send = Sink()
    bot = _Bot(env.c, env.sh, env.p)
    return Coordinator(bot, tg, env.j), bot


def test_stranger_cannot_approve_or_command(env):
    co, bot = _coord(env)
    s = sig()
    co.ask_approval("sid", s, env.sh.check(s, env.snap()))
    co.handle({"callback_query": {"id": "q", "data": "ok:sid", "message": {"chat": {"id": 999}}}})
    co.handle({"message": {"chat": {"id": 999}, "text": "/resume"}})
    co.handle({"message": {"chat": {"id": 999}, "text": "go live"}})
    assert bot.approved == [] and not env.j.get("go_live_confirmed")


def test_stop_command_is_instant_and_kills_pending(env):
    co, _ = _coord(env)
    s = sig()
    co.ask_approval("sid", s, env.sh.check(s, env.snap()))
    t0 = time.time()
    co.handle({"message": {"chat": {"id": 111}, "text": "/stop"}})
    assert time.time() - t0 < 1 and env.sh.halted() and co.pending == {}


def test_expired_signal_never_executes(env):
    co, bot = _coord(env)
    s = sig()
    co.ask_approval("sid", s, env.sh.check(s, env.snap()))
    co.pending["sid"]["expires"] = 0
    co.expire()
    co.handle({"callback_query": {"id": "q", "data": "ok:sid", "message": {"chat": {"id": 111}}}})
    assert bot.approved == []


def test_flat_closes_everything(env):
    env.go(sig("BTCUSDT"))
    env.go(sig("ETHUSDT", side="SHORT"))
    msg = env.p.flatten("telegram:111")
    assert "BTCUSDT" in msg and env.ex.positions() == {}


# ================================================================ end-to-end
def _bot(tmp_path, **kw):
    from madmac.bot import Bot
    tg = Telegram(token="", chat_id="111")
    sent = Sink()
    tg.send = sent
    ex = FakeExchange()
    b = Bot(cfg(**kw), ex=ex, journal=Journal(tmp_path / "j.db"), tg=tg)
    b.shield.lab = LAB_OK
    b.pilot.rules = dict(RULES)
    return b, ex, sent


def test_auto_mode_trades_without_tap_but_only_after_shield(tmp_path):
    b, ex, sent = _bot(tmp_path)
    b.on_signal(sig(), ex.equity(), ex.prices(), ex.positions())
    assert ex.limit_calls == 1
    b.shield.halt("uji")
    b.on_signal(sig("ETHUSDT"), ex.equity(), ex.prices(), ex.positions())
    assert ex.limit_calls == 1, "rem aktif → tidak ada order, juga di mode auto"


def test_per_trade_mode_waits_for_your_tap(tmp_path):
    b, ex, sent = _bot(tmp_path, approval="per_trade")
    s = sig()
    b.on_signal(s, ex.equity(), ex.prices(), ex.positions())
    assert ex.limit_calls == 0 and any("SINYAL BTCUSDT LONG" in m for m in sent.msgs)
    b.coord.handle({"callback_query": {"id": "q", "data": f"ok:{trade_id(s)}", "message": {"chat": {"id": 111}}}})
    assert ex.limit_calls == 1


def test_testnet_anchor_keeps_risk_geometry(tmp_path):
    b, ex, _ = _bot(tmp_path)
    s = sig()
    moved = b.anchor(s, {"BTCUSDT": (91_000.0, time.time())})
    assert moved.entry == 91_000.0
    assert moved.entry - moved.stop == pytest.approx(s.entry - s.stop)


def test_reports_render(tmp_path):
    b, ex, _ = _bot(tmp_path)
    r = b.daily_report()
    assert "LAPORAN HARIAN" in r and "Keputusan untuk Anda" in r
    assert "REVIEW MINGGUAN" in b.weekly_review()
    assert "Equity" in b.status_text()
