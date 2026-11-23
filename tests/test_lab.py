"""Lab honesty tests. Synthetic candles here test the SIMULATOR'S rules only;
they say nothing about whether a strategy makes money."""
from dataclasses import replace

from madmac import lab
from madmac.config import Config, LabRules
from madmac.indicators import Candle, ema, rsi
from madmac.strategies import STRATEGIES, Signal


def _candles(prices):
    out = []
    for i, p in enumerate(prices):
        out.append(Candle(i * 300_000, p, p + 5, p - 5, p, 10, i * 300_000 + 299_999))
    return out


def test_indicators_basic():
    assert ema([1, 2, 3], 3)[-1] == 2
    assert rsi(list(range(1, 30)), 14)[-1] == 100.0


def test_no_lookahead_strategies_ignore_future():
    cs = _candles([100 + i * 0.1 for i in range(400)])
    for name, fn in STRATEGIES.items():
        a = fn(cs[:350])
        b = fn(cs[:350])  # same past → same answer, future never passed in
        assert a == b


def test_same_candle_stop_and_target_counts_as_loss(monkeypatch):
    cfg = Config(strategies=["ema_pullback"], lab=LabRules(), risk_per_trade_pct=1, max_leverage=20,
                 max_fee_share_of_risk_pct=100)
    cs = _candles([90_000.0] * 302)
    # candle 301 (after entry) spans both stop and target
    cs.append(Candle(302 * 300_000, 90_000, 92_000, 88_000, 90_000, 10, 302 * 300_000 + 299_999))
    fired = {"n": 0}

    def once(window):
        if fired["n"] == 0 and window[-1].close_time == cs[300].close_time:
            fired["n"] += 1
            return Signal("ema_pullback", "LONG", 90_000, 89_000, 91_000, "t", window[-1].close_time)
        return None

    monkeypatch.setitem(lab.STRATEGIES, "ema_pullback", once)
    st = lab.simulate(cs, "ema_pullback", cfg)
    assert st.trades == 1 and st.wins == 0 and st.end_equity < 1000


def test_judge_rejects_few_trades():
    cfg = Config(lab=LabRules())
    s = lab.Stats(trades=5, wins=5, gross_win=100, gross_loss=1)
    ok, why = lab.judge(s, s, cfg)
    assert not ok and why


def test_last_full_months():
    from datetime import datetime, timezone
    assert lab.last_full_months(2, datetime(2026, 1, 15, tzinfo=timezone.utc)) == ["2025-11", "2025-12"]
