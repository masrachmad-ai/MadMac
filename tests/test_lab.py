"""Lab honesty tests. Synthetic candles test the LAB'S RULES only — they say
nothing about whether any strategy makes money on real markets."""
from __future__ import annotations

import random
from dataclasses import replace

import pytest

from madmac import lab
from madmac.config import Config, LabRules
from madmac.features import WARMUP, Ctx
from madmac.indicators import Candle, ema, rsi
from madmac.strategies import STRATEGIES, Signal, scan


def series(n=6000, seed=1, momentum=0.0, vol=0.002, oi=True):
    rng = random.Random(seed)
    p, prev, v0 = 100.0, 0.0, 1e6
    cs, ois = [], []
    for i in range(n):
        o = p
        r = momentum * prev + rng.gauss(0, vol)
        prev = r
        p *= 1 + r
        h = max(o, p) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, p) * (1 - abs(rng.gauss(0, vol / 2)))
        volu = rng.uniform(50, 150) * (1 + 50 * abs(r))
        cs.append(Candle(i * 300_000, o, h, lo, p, volu, i * 300_000 + 299_999, volu * rng.uniform(0.3, 0.7)))
        v0 *= 1 + rng.gauss(0, 0.003)
        ois.append((i * 300_000 + 299_999, v0))
    return cs, (ois if oi else None)


def cfg(**kw):
    return replace(Config(strategies=list(STRATEGIES), lab=LabRules(random_baseline_draws=1500,
                                                                     monte_carlo_runs=300)), **kw)


def test_indicators_basic():
    assert ema([1, 2, 3], 3)[-1] == 2
    assert rsi(list(range(1, 30)), 14)[-1] == 100.0


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_no_lookahead_signal_never_changes_when_future_arrives(name):
    """The signal at candle i must be identical whether or not later candles
    exist. This is the strongest guard against 'predicting' with hindsight."""
    cs, oi = series(1500, seed=4, vol=0.004)
    full = scan(name, Ctx(cs, "X", oi=oi, funding=[(0, 0.0001)]))
    for cut in (400, 650, 900, 1200, 1499):
        part = scan(name, Ctx(cs[:cut + 1], "X", oi=[p for p in oi if p[0] <= cs[cut].close_time],
                              funding=[(0, 0.0001)]))
        assert part[cut] == full[cut], f"{name} berubah di candle {cut} setelah data masa depan ditambah"


def test_lab_rejects_pure_noise():
    """Random-walk prices have no edge. Every pair must FAIL."""
    cs, oi = series(30000, seed=1)
    out = lab.run(cfg(), ctxs={"NOISEUSDT": Ctx(cs, "NOISEUSDT", oi=oi, funding=[(0, 0.0001)])},
                  out_dir=__import__("pathlib").Path("/tmp/madmac-lab-noise"), log=lambda *a: None)
    assert out["pairs_tested"] == len(STRATEGIES)
    assert not any(v["passed"] for v in out["pairs"].values())


def test_lab_passes_a_real_edge():
    """Prices with genuine momentum DO have an edge for trend rules: the Lab
    must be able to say yes when the evidence is there."""
    cs, _ = series(30000, seed=3, momentum=0.35, vol=0.002)
    c = cfg(strategies=["range_breakout"], entry_order="taker")
    out = lab.run(c, ctxs={"MOMUSDT": Ctx(cs, "MOMUSDT")},
                  out_dir=__import__("pathlib").Path("/tmp/madmac-lab-mom"), log=lambda *a: None)
    v = out["pairs"]["range_breakout|MOMUSDT"]
    assert v["passed"], v["why_not"]


def test_missing_open_interest_is_reported_not_faked():
    cs, _ = series(2000, oi=False)
    r = lab.evaluate_pair(Ctx(cs, "X"), "bos_oi", cfg())
    assert r.why_not == ["data open interest tidak tersedia"]


def _one_signal_ctx(next_open, next_high, next_low, after=None):
    cs = [Candle(i * 300_000, 100, 100.05, 99.95, 100, 10, i * 300_000 + 299_999, 5) for i in range(WARMUP + 2)]
    cs.append(Candle(0, next_open, next_high, next_low, next_open, 10, 0, 5))
    for a in after or []:
        cs.append(Candle(0, *a, 10, 0, 5))
    return Ctx(cs, "X")


def _sig(ctx, i):
    return Signal("t", "LONG", 100.0, 99.0, 102.0, "", ctx.t[i], "X", 10)


def test_maker_fill_needs_trade_through_and_gap_is_rejected():
    c = cfg(max_fee_share_of_risk_pct=100)
    i = WARMUP + 1
    touch = _one_signal_ctx(100.2, 100.3, 100.0)            # touches 100 but not below
    assert lab.simulate_one(touch, i, _sig(touch, i), c) is None
    gap = _one_signal_ctx(99.9, 100.1, 99.5)                # opens BELOW limit → post-only rejected
    assert lab.simulate_one(gap, i, _sig(gap, i), c) is None
    ok = _one_signal_ctx(100.2, 100.3, 99.9, after=[(100, 102.5, 99.95, 102.4)])
    k, r = lab.simulate_one(ok, i, _sig(ok, i), c)
    assert r > 0


def test_stop_beats_target_in_same_candle():
    c = cfg(max_fee_share_of_risk_pct=100)
    i = WARMUP + 1
    ctx = _one_signal_ctx(100.2, 100.3, 99.9, after=[(100, 103, 98.5, 100)])
    k, r = lab.simulate_one(ctx, i, _sig(ctx, i), c)
    assert r < -0.99


def test_benjamini_hochberg():
    assert lab.benjamini_hochberg({"a": 0.001, "b": 0.02, "c": 0.5}, 0.1) == {"a", "b"}
    assert lab.benjamini_hochberg({"a": 0.2, "b": 0.3}, 0.1) == set()


def test_monte_carlo_drawdown_is_worse_than_lucky_order():
    # alternating win/loss is the luckiest order; shuffles produce losing streaks
    tr = [lab.Trade(i, "LONG", (1 if i % 2 else -1), (0.01 if i % 2 else -0.01)) for i in range(40)]
    assert lab.monte_carlo_dd(tr, 300, 1) > 2 * lab.Stats.of(tr).max_dd_pct


def test_last_full_months():
    from datetime import datetime, timezone
    assert lab.last_full_months(2, datetime(2026, 1, 15, tzinfo=timezone.utc)) == ["2025-11", "2025-12"]
