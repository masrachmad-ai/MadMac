"""A fake Binance used ONLY in tests. It simulates failures on purpose."""
from __future__ import annotations

import time

import requests

from madmac.binance import BinanceError, OrderUnknown
from madmac.shield import SymbolRules

RULES = {"BTCUSDT": SymbolRules(0.001, 0.001, 100, 0.1),
         "ETHUSDT": SymbolRules(0.001, 0.001, 20, 0.01),
         "SOLUSDT": SymbolRules(0.01, 0.01, 5, 0.01),
         "XRPUSDT": SymbolRules(0.1, 0.1, 5, 0.0001)}
PRICES = {"BTCUSDT": 90_000.0, "ETHUSDT": 3_000.0, "SOLUSDT": 150.0, "XRPUSDT": 2.0}


class FakeExchange:
    def __init__(self, equity=10_000.0):
        self._equity = equity
        self.px = dict(PRICES)
        self.price_ts = time.time()
        self.pos: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}       # client id → order
        self.prot: dict[str, list] = {}         # symbol → protective orders
        self.market_calls = 0
        self.limit_calls = 0
        self.fill_ratio = 1.0                   # maker: fraction filled when fill() called
        self.auto_fill_limits = False           # fill limit entries immediately
        self.gtx_reject = False                 # post-only would cross → EXPIRED
        self.timeout_on_order = False
        self.order_lands_anyway = True
        self.reject_protective = False
        self.fills: dict[str, list] = {}
        self.leverage = {}

    # ---------------- market data
    def price(self, symbol):
        return self.px[symbol], self.price_ts

    def prices(self):
        return {s: (p, self.price_ts) for s, p in self.px.items()}

    def server_time(self):
        return time.time()

    def symbol_rules_all(self, symbols):
        return {s: RULES[s] for s in symbols if s in RULES}

    def sync_time(self):
        pass

    def klines(self, symbol, interval, limit):
        return []

    def open_interest_hist(self, symbol, period, limit):
        return []

    def funding_now(self, symbol):
        return int(time.time() * 1000), 0.0001

    # ---------------- account
    def equity(self):
        return self._equity

    def positions(self):
        return {s: dict(p) for s, p in self.pos.items() if p["qty"] != 0}

    def position(self, symbol):
        return dict(self.pos.get(symbol, {"qty": 0.0, "entry": 0.0}))

    def set_leverage(self, symbol, lev):
        self.leverage[symbol] = lev

    def set_margin_type(self, symbol, mt):
        pass

    # ---------------- orders
    def _apply(self, symbol, side, qty, reduce_only, price):
        cur = self.pos.get(symbol, {"qty": 0.0, "entry": 0.0})
        if reduce_only:
            self.pos[symbol] = {"qty": 0.0, "entry": 0.0}
            self.prot[symbol] = []
        elif qty:
            signed = qty if side == "BUY" else -qty
            self.pos[symbol] = {"qty": round(cur["qty"] + signed, 8), "entry": price}

    def market_order(self, symbol, side, qty, client_id, reduce_only=False):
        self.market_calls += 1
        if client_id in self.orders:
            raise BinanceError(400, -4116, "Duplicate clientOrderId")
        o = {"clientOrderId": client_id, "symbol": symbol, "side": side, "origQty": str(qty),
             "executedQty": str(qty), "avgPrice": str(self.px[symbol]), "status": "FILLED", "type": "MARKET"}
        if self.timeout_on_order:
            if self.order_lands_anyway:
                self._apply(symbol, side, qty, reduce_only, self.px[symbol])
                self.orders[client_id] = o
                raise _Timeout(client_id)
            raise OrderUnknown("timeout")
        self._apply(symbol, side, qty, reduce_only, self.px[symbol])
        self.orders[client_id] = o
        return o

    def limit_order(self, symbol, side, qty, price, client_id, reduce_only=False):
        if client_id in self.orders:
            raise BinanceError(400, -4116, "Duplicate clientOrderId")
        if reduce_only:
            o = {"clientOrderId": client_id, "status": "NEW", "executedQty": "0", "type": "LIMIT"}
            self.orders[client_id] = o
            self.prot.setdefault(symbol, []).append(
                {"type": "TP_LIMIT", "trigger": price, "client_id": client_id, "id": client_id, "algo": False})
            return o
        self.limit_calls += 1
        o = {"clientOrderId": client_id, "symbol": symbol, "side": side, "origQty": str(qty),
             "executedQty": "0", "avgPrice": "0", "status": "NEW", "price": str(price), "type": "LIMIT"}
        if self.gtx_reject:
            o["status"] = "EXPIRED"
        self.orders[client_id] = o
        if self.auto_fill_limits and not self.gtx_reject:
            self.fill(client_id)
        return dict(o)

    def fill(self, client_id, ratio=None):
        """Test helper: the market trades through our limit."""
        o = self.orders[client_id]
        ratio = self.fill_ratio if ratio is None else ratio
        want = float(o["origQty"])
        filled = round(want * ratio, 8)
        prev = float(o["executedQty"])
        o["executedQty"] = str(filled)
        o["avgPrice"] = o["price"]
        o["status"] = "FILLED" if filled >= want else "PARTIALLY_FILLED"
        self._apply(o["symbol"], o["side"], filled - prev, False, float(o["price"]))

    def get_order(self, symbol, client_id):
        if client_id not in self.orders:
            raise BinanceError(400, -2013, "Order does not exist.")
        return dict(self.orders[client_id])

    def cancel_order(self, symbol, client_id):
        o = self.orders.get(client_id)
        if o and o["status"] in ("NEW", "PARTIALLY_FILLED"):
            o["status"] = "CANCELED"

    def protective_order(self, symbol, side, kind, trigger, client_id):
        if self.reject_protective:
            raise BinanceError(400, -4000, "rejected")
        o = {"type": kind, "trigger": trigger, "client_id": client_id, "id": client_id, "algo": True}
        self.prot.setdefault(symbol, []).append(o)
        return o

    def open_protective_orders(self, symbol):
        return list(self.prot.get(symbol, []))

    def cancel_all(self, symbol):
        self.prot[symbol] = []
        for o in self.orders.values():
            if o.get("symbol") == symbol and o["status"] in ("NEW", "PARTIALLY_FILLED"):
                o["status"] = "CANCELED"

    def user_trades(self, symbol, start_ms):
        return self.fills.get(symbol, [])

    # test helper: the stop is hit on the exchange
    def hit_stop(self, symbol, exit_price, pnl, fee):
        self.pos[symbol] = {"qty": 0.0, "entry": 0.0}
        self.prot[symbol] = []
        self.fills[symbol] = [{"price": str(exit_price), "qty": "1", "realizedPnl": str(pnl), "commission": str(fee)}]


class _Timeout(requests.Timeout):
    def __init__(self, cid):
        super().__init__("read timed out")
        self.cid = cid


class TimeoutAwareExchange(FakeExchange):
    """Like the real client: on timeout, look the order up by its id."""

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
