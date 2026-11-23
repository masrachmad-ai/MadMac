"""Journal: the bot's memory. Every signal, Shield decision, order, fill,
trade result and system event is written here with its reason and a UTC
timestamp. Shield reads today's results from here, so the journal is also
the source of truth for the daily limits."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL,
  agent TEXT NOT NULL, message TEXT NOT NULL, data TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, status TEXT NOT NULL,
  placed_ts INTEGER NOT NULL, opened_ts INTEGER, closed_ts INTEGER,
  mode TEXT NOT NULL, strategy TEXT NOT NULL, side TEXT NOT NULL,
  qty REAL NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL,
  max_hold_s INTEGER NOT NULL DEFAULT 0, tp_placed INTEGER NOT NULL DEFAULT 0,
  exit REAL, pnl_usdt REAL, fees_usdt REAL, reason TEXT, exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def utc_day_start(ts: float | None = None) -> int:
    d = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ---------- events ----------
    def log(self, agent: str, kind: str, message: str, **data) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO events(ts,kind,agent,message,data) VALUES(?,?,?,?,?)",
                (int(time.time()), kind, agent, message, json.dumps(data, default=str) if data else None),
            )

    def events_since(self, ts: int, kinds: tuple[str, ...] | None = None) -> list[dict]:
        q = "SELECT ts,kind,agent,message,data FROM events WHERE ts>=?"
        args: list = [ts]
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [dict(ts=r[0], kind=r[1], agent=r[2], message=r[3],
                     data=json.loads(r[4]) if r[4] else {}) for r in rows]

    # ---------- state (kill switch, go-live, etc.) ----------
    def get(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k: str, v) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO state(k,v) VALUES(?,?)", (k, json.dumps(v)))

    # ---------- trades ----------
    # status: pending (limit order waiting) → open (filled, protected) → closed
    #         pending → cancelled (never filled)
    def add_trade(self, tid: str, symbol: str, mode: str, strategy: str, side: str, qty: float,
                  entry: float, stop: float, target: float, reason: str, max_hold_s: int,
                  status: str = "pending") -> None:
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO trades(id,symbol,status,placed_ts,opened_ts,mode,strategy,side,qty,entry,"
                "stop,target,max_hold_s,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, symbol, status, now, now if status == "open" else None, mode, strategy, side, qty,
                 entry, stop, target, max_hold_s, reason))

    def mark_open(self, tid: str, qty: float, entry: float) -> None:
        with self.lock:
            self.db.execute("UPDATE trades SET status='open', qty=?, entry=?, opened_ts=COALESCE(opened_ts,?)"
                            " WHERE id=?", (qty, entry, int(time.time()), tid))

    def mark_tp(self, tid: str) -> None:
        with self.lock:
            self.db.execute("UPDATE trades SET tp_placed=1 WHERE id=?", (tid,))

    def mark_cancelled(self, tid: str, why: str) -> None:
        with self.lock:
            self.db.execute("UPDATE trades SET status='cancelled', closed_ts=?, exit_reason=? WHERE id=?",
                            (int(time.time()), why, tid))

    def close_trade(self, tid: str, exit_price: float, pnl: float, fees: float, why: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE trades SET status='closed', closed_ts=?,exit=?,pnl_usdt=?,fees_usdt=?,exit_reason=?"
                " WHERE id=? AND status IN ('open','pending')",
                (int(time.time()), exit_price, pnl, fees, why, tid))

    def trade(self, tid: str) -> dict | None:
        r = self._trades("id=?", tid)
        return r[0] if r else None

    def active_trades(self) -> list[dict]:
        """pending + open: everything that holds or may soon hold risk."""
        return self._trades("status IN ('pending','open')")

    def open_trades(self) -> list[dict]:
        return self._trades("status='open'")

    def closed_since(self, ts: int) -> list[dict]:
        return self._trades("status='closed' AND closed_ts>=?", ts)

    def trades_opened_since(self, ts: int) -> list[dict]:
        return self._trades("status IN ('open','closed') AND opened_ts>=?", ts)

    def _trades(self, where: str, *args) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM trades WHERE {where} ORDER BY placed_ts, rowid", args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------- numbers Shield needs ----------
    def realized_pnl_today(self, now: float | None = None) -> float:
        return sum(t["pnl_usdt"] or 0 for t in self.closed_since(utc_day_start(now)))

    def consecutive_losses(self) -> int:
        """Losing streak across days; resets only on a winning trade."""
        rows = self.db.execute(
            "SELECT pnl_usdt FROM trades WHERE status='closed' ORDER BY closed_ts DESC, rowid DESC LIMIT 50"
        ).fetchall()
        n = 0
        for (p,) in rows:
            if p is not None and p < 0:
                n += 1
            else:
                break
        return n

    # ---------- reviews ----------
    def summary(self, since_ts: int) -> dict:
        tr = self.closed_since(since_ts)
        wins = [t for t in tr if (t["pnl_usdt"] or 0) > 0]
        gross_win = sum(t["pnl_usdt"] for t in wins)
        gross_loss = -sum(t["pnl_usdt"] for t in tr if (t["pnl_usdt"] or 0) < 0)
        by_strat: dict[str, float] = {}
        by_sym: dict[str, float] = {}
        for t in tr:
            by_strat[t["strategy"]] = by_strat.get(t["strategy"], 0) + (t["pnl_usdt"] or 0)
            by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0) + (t["pnl_usdt"] or 0)
        return dict(
            trades=len(tr), wins=len(wins),
            win_rate=(len(wins) / len(tr) * 100) if tr else 0.0,
            pnl=sum(t["pnl_usdt"] or 0 for t in tr),
            fees=sum(t["fees_usdt"] or 0 for t in tr),
            profit_factor=(gross_win / gross_loss) if gross_loss else None,
            by_strategy=by_strat, by_symbol=by_sym,
            unfilled=len(self._trades("status='cancelled' AND closed_ts>=?", since_ts)),
            blocked=len(self.events_since(since_ts, ("shield_block",))),
        )

    def week_start(self) -> int:
        return utc_day_start() - 6 * 86400
