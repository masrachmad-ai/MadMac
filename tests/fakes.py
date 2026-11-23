"""A fake Binance used ONLY in tests. It simulates failures on purpose."""
from __future__ import annotations

import time

import requests

from madmac.binance import BinanceError, OrderUnknown
from madmac.shield import SymbolRules

RULES = SymbolRules(step_size=0.001, min_qty=0.001, min_notional=100, tick_size=0.1)


class FakeExchange:
    def __init__(self, equity=1000.0, price=90_000.0):
        self._equity = equity
        self._price = price
        self.price_ts = time.time()
        self.pos = {"qty": 0.0, "entry": 0.0}
        self.orders: dict[str, dict] = {}       # client id → order
        self.protective: list[dict] = []
        self.market_calls = 0
        self.fill_ratio = 1.0                   # <1 → partial fill
        self.timeout_on_order = False           # request times out…
        self.order_lands_anyway = True          # …but did it reach Binance?
        self.reject_protective = False
        self.fills: list[dict] = []
        self.leverage = None

    # market data
    def price(self, symbol):
        return self._price, self.price_ts

    def server_time(self):
        return time.time()

    def symbol_rules(self, symbol):
        return RULES

    def sync_time(self):
        pass

    # account
    def equity(self):
        return self._equity

    def position(self, symbol):
        return dict(self.pos)

    def set_leverage(self, symbol, lev):
        self.leverage = lev

    def set_margin_type(self, symbol, mt):
        pass

    # orders
    def market_order(self, symbol, side, qty, client_id, reduce_only=False):
        self.market_calls += 1
        if client_id in self.orders:
            raise BinanceError(400, -4116, "Duplicate clientOrderId")
        filled = round(qty * self.fill_ratio, 3) if not reduce_only else qty
        o = {"clientOrderId": client_id, "executedQty": str(filled), "avgPrice": str(self._price),
             "status": "FILLED" if filled == qty else ("PARTIALLY_FILLED" if filled else "EXPIRED")}
        if self.timeout_on_order:
            if self.order_lands_anyway:
                self._apply(side, filled, reduce_only)
                self.orders[client_id] = o
            raise OrderUnknown("timeout") if not self.order_lands_anyway else _Timeout(self, client_id)
        self._apply(side, filled, reduce_only)
        self.orders[client_id] = o
        return o

    def _apply(self, side, qty, reduce_only):
        signed = qty if side == "BUY" else -qty
        if reduce_only:
            self.pos = {"qty": 0.0, "entry": 0.0}
            self.protective.clear()
        elif qty:
            self.pos = {"qty": self.pos["qty"] + signed, "entry": self._price}

    def get_order(self, symbol, client_id):
        if client_id not in self.orders:
            raise BinanceError(400, -2013, "Order does not exist.")
        return self.orders[client_id]

    def protective_order(self, symbol, side, kind, trigger, client_id):
        if self.reject_protective:
            raise BinanceError(400, -4000, "rejected")
        o = {"type": kind, "trigger": trigger, "client_id": client_id, "id": len(self.protective), "algo": True}
        self.protective.append(o)
        return o

    def open_protective_orders(self, symbol):
        return list(self.protective)

    def cancel_all(self, symbol):
        self.protective.clear()

    def user_trades(self, symbol, start_ms):
        return self.fills

    def klines(self, symbol, interval, limit):
        return []

    # test helper: simulate stop being hit on the exchange
    def hit_stop(self, exit_price, pnl, fee):
        self.pos = {"qty": 0.0, "entry": 0.0}
        self.protective.clear()
        self.fills = [{"price": str(exit_price), "qty": "0.01", "realizedPnl": str(pnl), "commission": str(fee)}]


class _Timeout(requests.Timeout):
    """Raised when the order DID land but the response was lost; the real
    client then looks the order up — emulate that path."""

    def __init__(self, ex, cid):
        super().__init__("read timed out")
        self.ex, self.cid = ex, cid


class TimeoutAwareExchange(FakeExchange):
    """Wraps market_order like the real client: on timeout, look up by id."""

    def market_order(self, symbol, side, qty, client_id, reduce_only=False):
        try:
            return super().market_order(symbol, side, qty, client_id, reduce_only)
        except _Timeout:
            return self.get_order(symbol, client_id)


class Sink:
    def __init__(self):
        self.msgs: list[str] = []

    def __call__(self, text, buttons=None):
        self.msgs.append(text)

    send = __call__
