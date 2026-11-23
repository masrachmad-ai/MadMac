"""Thin Binance USDⓈ-M Futures REST client.

Safety properties:
* API keys are read from environment variables only — never from config,
  never logged, never sent anywhere except Binance in the request header.
* Every request has a timeout. Read-only requests are retried; ORDER requests
  are never blindly retried: after a timeout we look the order up by its
  client id, so a network hiccup can't create a duplicate order.
* Clock drift (error -1021) triggers a server-time resync.

Endpoint names were written from Binance docs as known at build time and
could not be re-verified from the build environment. `python -m madmac
selfcheck` exercises every call on the testnet before the bot is trusted.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import requests

from .indicators import Candle
from .shield import SymbolRules

BASE = {
    "testnet": "https://testnet.binancefuture.com",
    "live": "https://fapi.binance.com",
}
TIMEOUT = 10


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"HTTP {status} code {code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


class OrderUnknown(Exception):
    """Order request timed out and we could not confirm whether it exists."""


def _num(x: float) -> str:
    """Decimal string without float noise (Binance rejects 0.30000000000000004)."""
    return f"{x:.10f}".rstrip("0").rstrip(".")


class Binance:
    def __init__(self, mode: str, session: requests.Session | None = None,
                 key: str | None = None, secret: str | None = None):
        if mode not in BASE:
            raise ValueError(mode)
        self.mode = mode
        self.base = BASE[mode]
        prefix = "BINANCE_TESTNET" if mode == "testnet" else "BINANCE_LIVE"
        self.key = key if key is not None else os.environ.get(f"{prefix}_API_KEY", "")
        self.secret = secret if secret is not None else os.environ.get(f"{prefix}_API_SECRET", "")
        self.s = session or requests.Session()
        self.time_offset_ms = 0

    # ------------------------------------------------------------ plumbing
    def sync_time(self) -> None:
        t0 = time.time() * 1000
        server = self._request("GET", "/fapi/v1/time")["serverTime"]
        t1 = time.time() * 1000
        self.time_offset_ms = int(server - (t0 + t1) / 2)

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False, retries: int = 2):
        params = {k: (_num(v) if isinstance(v, float) else v) for k, v in (params or {}).items()}
        for attempt in range(retries + 1):
            q = dict(params)
            headers = {}
            if signed:
                if not self.key or not self.secret:
                    raise BinanceError(0, None, f"API key {self.mode} belum dipasang di server")
                q["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
                q["recvWindow"] = 5000
                qs = urlencode(q)
                sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
                url = f"{self.base}{path}?{qs}&signature={sig}"
                headers["X-MBX-APIKEY"] = self.key
            else:
                url = f"{self.base}{path}" + (f"?{urlencode(q)}" if q else "")
            try:
                r = self.s.request(method, url, headers=headers, timeout=TIMEOUT)
            except (requests.Timeout, requests.ConnectionError):
                if attempt < retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise
            if r.status_code == 200:
                return r.json()
            try:
                body = r.json()
                code, msg = body.get("code"), body.get("msg", r.text)
            except ValueError:
                code, msg = None, r.text
            if code == -1021 and attempt < retries:  # clock drift
                self.sync_time()
                continue
            if r.status_code in (429, 418, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.0 * 2 ** attempt)
                continue
            raise BinanceError(r.status_code, code, msg)

    # ------------------------------------------------------------ market data
    def klines(self, symbol: str, interval: str, limit: int = 1000) -> list[Candle]:
        rows = self._request("GET", "/fapi/v1/klines",
                             {"symbol": symbol, "interval": interval, "limit": limit})
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                       float(r[5]), int(r[6]), float(r[9])) for r in rows]

    def price(self, symbol: str) -> tuple[float, float]:
        """(price, observed_at_unix_seconds). Uses Binance's own timestamp."""
        r = self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        ts = r.get("time")
        return float(r["price"]), (ts / 1000 if ts else time.time())

    def prices(self) -> dict[str, tuple[float, float]]:
        rows = self._request("GET", "/fapi/v1/ticker/price")
        now = time.time()
        return {r["symbol"]: (float(r["price"]), r["time"] / 1000 if r.get("time") else now) for r in rows}

    def server_time(self) -> float:
        return self._request("GET", "/fapi/v1/time")["serverTime"] / 1000

    def open_interest_hist(self, symbol: str, period: str, limit: int = 200) -> list[tuple[int, float]]:
        """Open interest (USDT value) history. Binance keeps only ~30 days here."""
        rows = self._request("GET", "/futures/data/openInterestHist",
                             {"symbol": symbol, "period": period, "limit": limit})
        return [(int(r["timestamp"]), float(r["sumOpenInterestValue"])) for r in rows]

    def funding_now(self, symbol: str) -> tuple[int, float]:
        r = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return int(r.get("time") or time.time() * 1000), float(r["lastFundingRate"])

    def symbol_rules_all(self, symbols: list[str]) -> dict[str, SymbolRules]:
        info = self._request("GET", "/fapi/v1/exchangeInfo")
        out = {}
        for s in info["symbols"]:
            if s["symbol"] not in symbols:
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            out[s["symbol"]] = SymbolRules(
                step_size=float(lot["stepSize"]), min_qty=float(lot["minQty"]),
                min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                tick_size=float(f["PRICE_FILTER"]["tickSize"]))
        return out

    def symbol_rules(self, symbol: str) -> SymbolRules:
        return self.symbol_rules_all([symbol])[symbol]

    # ------------------------------------------------------------ account
    def equity(self) -> float:
        try:
            a = self._request("GET", "/fapi/v3/account", signed=True)
        except BinanceError as e:
            if e.status != 404:
                raise
            a = self._request("GET", "/fapi/v2/account", signed=True)
        return float(a["totalMarginBalance"])

    def positions(self) -> dict[str, dict]:
        """All non-zero positions: {symbol: {'qty': signed, 'entry': price}}."""
        try:
            rows = self._request("GET", "/fapi/v3/positionRisk", signed=True)
        except BinanceError as e:
            if e.status != 404:
                raise
            rows = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return {p["symbol"]: {"qty": float(p["positionAmt"]), "entry": float(p["entryPrice"])}
                for p in rows if float(p["positionAmt"]) != 0}

    def position(self, symbol: str) -> dict:
        return self.positions().get(symbol, {"qty": 0.0, "entry": 0.0})

    def set_leverage(self, symbol: str, lev: int) -> None:
        self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev}, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self._request("POST", "/fapi/v1/marginType",
                          {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceError as e:
            if e.code != -4046:  # "No need to change margin type."
                raise

    # ------------------------------------------------------------ orders
    def _place_idempotent(self, params: dict, symbol: str, client_id: str) -> dict:
        try:
            return self._request("POST", "/fapi/v1/order", params, signed=True, retries=0)
        except (requests.Timeout, requests.ConnectionError) as e:
            for _ in range(5):
                time.sleep(1)
                try:
                    return self.get_order(symbol, client_id)
                except BinanceError as be:
                    if be.code == -2013:  # order does not exist
                        continue
                    raise
                except (requests.Timeout, requests.ConnectionError):
                    continue
            raise OrderUnknown(f"Order {client_id} tidak bisa dikonfirmasi") from e

    def market_order(self, symbol: str, side: str, qty: float, client_id: str,
                     reduce_only: bool = False) -> dict:
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
                  "newClientOrderId": client_id, "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._place_idempotent(params, symbol, client_id)

    def limit_order(self, symbol: str, side: str, qty: float, price: float, client_id: str,
                    reduce_only: bool = False) -> dict:
        """Post-only (GTX) limit: rests in the book as MAKER or is rejected
        (status EXPIRED) — it can never cross the spread and pay taker."""
        params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "GTX",
                  "quantity": qty, "price": price, "newClientOrderId": client_id,
                  "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._place_idempotent(params, symbol, client_id)

    def get_order(self, symbol: str, client_id: str) -> dict:
        return self._request("GET", "/fapi/v1/order",
                             {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def cancel_order(self, symbol: str, client_id: str) -> None:
        try:
            self._request("DELETE", "/fapi/v1/order",
                          {"symbol": symbol, "origClientOrderId": client_id}, signed=True)
        except BinanceError as e:
            if e.code != -2011:  # unknown order (already filled/cancelled)
                raise

    def protective_order(self, symbol: str, side: str, kind: str, trigger: float,
                         client_id: str) -> dict:
        """Place a closePosition STOP_MARKET / TAKE_PROFIT_MARKET that lives on
        Binance's servers. Tries the Algo Order endpoint first (Binance moved
        conditional orders there in late 2025), falls back to the classic
        order endpoint."""
        try:
            return self._request("POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": kind,
                "triggerPrice": trigger, "closePosition": "true",
                "workingType": "MARK_PRICE", "priceProtect": "true",
                "clientAlgoId": client_id}, signed=True, retries=0)
        except BinanceError as e:
            if e.status not in (400, 404) or e.code in (-2021,):  # -2021: would trigger immediately
                raise
        return self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": kind, "stopPrice": trigger,
            "closePosition": "true", "workingType": "MARK_PRICE", "priceProtect": "true",
            "newClientOrderId": client_id}, signed=True, retries=0)

    def open_protective_orders(self, symbol: str) -> list[dict]:
        """Resting stop / take-profit orders from both endpoints, normalised to
        {'type','trigger','client_id','id','algo'}. A reduce-only LIMIT order
        is reported as type 'TP_LIMIT'."""
        out = []
        try:
            for o in self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True) or []:
                out.append({"type": o.get("orderType") or o.get("type"),
                            "trigger": float(o.get("triggerPrice") or 0),
                            "client_id": o.get("clientAlgoId"), "id": o.get("algoId"), "algo": True})
        except BinanceError as e:
            if e.status not in (400, 404):
                raise
        for o in self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True):
            t = o.get("type")
            if t in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                out.append({"type": t, "trigger": float(o.get("stopPrice") or 0),
                            "client_id": o.get("clientOrderId"), "id": o.get("orderId"), "algo": False})
            elif t == "LIMIT" and o.get("reduceOnly"):
                out.append({"type": "TP_LIMIT", "trigger": float(o.get("price") or 0),
                            "client_id": o.get("clientOrderId"), "id": o.get("orderId"), "algo": False})
        return out

    def cancel_all(self, symbol: str) -> None:
        for o in self.open_protective_orders(symbol):
            if o["algo"]:
                try:
                    self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": o["id"]}, signed=True)
                except BinanceError:
                    pass
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def user_trades(self, symbol: str, start_ms: int) -> list[dict]:
        return self._request("GET", "/fapi/v1/userTrades",
                             {"symbol": symbol, "startTime": start_ms, "limit": 1000}, signed=True)
