"""Load and validate config.yaml. Bad config = bot refuses to start."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MADMAC_DATA_DIR", ROOT / "data"))

VALID_TF = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}


class ConfigError(ValueError):
    pass


@dataclass
class LabRules:
    months_of_data: int = 6
    out_of_sample_pct: float = 30
    min_trades: int = 100
    min_profit_factor: float = 1.15
    max_drawdown_pct: float = 20
    min_win_rate_pct: float = 0


@dataclass
class Config:
    mode: str = "testnet"
    symbol: str = "BTCUSDT"
    timeframe: str = "5m"
    approval: str = "per_trade"
    approval_timeout_seconds: int = 90
    max_entry_drift_pct_of_stop: float = 30
    risk_per_trade_pct: float = 0.5
    max_leverage: int = 10
    margin_type: str = "ISOLATED"
    daily_loss_limit_pct: float = 3.0
    max_consecutive_losses: int = 3
    max_open_positions: int = 1
    max_trades_per_day: int = 20
    max_fee_share_of_risk_pct: float = 25
    taker_fee_pct: float = 0.05
    slippage_pct: float = 0.02
    max_feed_age_seconds: int = 30
    close_if_stop_fails: bool = True
    strategies: list = field(default_factory=lambda: ["ema_pullback"])
    lab: LabRules = field(default_factory=LabRules)
    daily_report_hour_wib: int = 7
    weekly_review_day: str = "sunday"

    @property
    def tf_seconds(self) -> int:
        return VALID_TF[self.timeframe]

    def validate(self) -> "Config":
        errs = []
        if self.mode not in ("testnet", "live"):
            errs.append("mode harus 'testnet' atau 'live'")
        if self.timeframe not in VALID_TF:
            errs.append(f"timeframe harus salah satu dari {sorted(VALID_TF)}")
        if self.approval not in ("per_trade", "auto"):
            errs.append("approval harus 'per_trade' atau 'auto'")
        if not 0 < self.risk_per_trade_pct <= 5:
            errs.append("risk_per_trade_pct harus di antara 0 dan 5")
        if not 1 <= self.max_leverage <= 125:
            errs.append("max_leverage harus 1–125")
        if self.margin_type not in ("ISOLATED", "CROSSED"):
            errs.append("margin_type harus ISOLATED atau CROSSED")
        if not 0 < self.daily_loss_limit_pct <= 20:
            errs.append("daily_loss_limit_pct harus 0–20")
        if self.max_consecutive_losses < 1:
            errs.append("max_consecutive_losses minimal 1")
        if self.max_open_positions != 1:
            errs.append("max_open_positions: versi ini hanya mendukung 1")
        if self.max_feed_age_seconds < 5:
            errs.append("max_feed_age_seconds minimal 5")
        from .strategies import STRATEGIES
        bad = [s for s in self.strategies if s not in STRATEGIES]
        if bad:
            errs.append(f"strategi tidak dikenal: {bad}. Pilihan: {sorted(STRATEGIES)}")
        if errs:
            raise ConfigError("config.yaml salah:\n - " + "\n - ".join(errs))
        return self


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("MADMAC_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text()) or {}
    lab = LabRules(**(raw.pop("lab", None) or {}))
    known = set(Config.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"config.yaml berisi kunci tidak dikenal: {sorted(unknown)}")
    return Config(**raw, lab=lab).validate()
