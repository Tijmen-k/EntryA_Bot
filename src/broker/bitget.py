"""
Bitget USDT-M Futures authenticated REST client.
Handles account queries, order placement, and position management.

Authentication (API v2):
  timestamp  = unix milliseconds (string)
  message    = timestamp + METHOD + requestPath + body
  signature  = Base64( HMAC-SHA256( secret, message ) )
  headers    = { ACCESS-KEY, ACCESS-SIGN, ACCESS-TIMESTAMP, ACCESS-PASSPHRASE }

For GET requests requestPath includes the query string.
For POST requests requestPath is the path only; body is the JSON string.
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from loguru import logger

import config


# ──────────────────────────────────────────────────────
# Data classes (same public interface as the old Kraken broker)
# ──────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    side: str           # "long" | "short"
    size: float         # ETH (converted from contracts)
    entry_price: float
    unrealised_pnl: float = 0.0


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    order_type: str     # "market" | "limit" | "stop"
    size: float         # ETH
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = "open"


# ──────────────────────────────────────────────────────
# Broker
# ──────────────────────────────────────────────────────

class BitgetBroker:
    """Authenticated REST client for Bitget USDT-M Futures."""

    _REST_PREFIX = "/api/v2/mix"

    def __init__(self) -> None:
        self._base        = config.BITGET_BASE_URL
        self._key         = config.BITGET_API_KEY
        self._secret      = config.BITGET_API_SECRET
        self._passphrase  = config.BITGET_API_PASSPHRASE
        self._dry_run     = config.DRY_RUN
        self.last_error   = ""   # last API error message, readable by callers
        self._session     = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "locale": "en-US",
        })
        if config.TRADING_MODE == "demo":
            self._session.headers["paptrading"] = "1"
        # Tracks which order IDs were placed as plan (stop-trigger) orders
        self._plan_orders: set[str] = set()

        if self._dry_run:
            logger.warning("DRY RUN MODE — no real orders will be placed")
        if not self._key or not self._secret or not self._passphrase:
            logger.warning("Bitget credentials incomplete — only public endpoints will work")

    # ──────────────────────────────────────────
    # Account
    # ──────────────────────────────────────────

    def get_account_balance(self) -> float:
        """Return available USDT margin for trading."""
        params = {
            "symbol": config.SYMBOL,
            "marginCoin": config.MARGIN_COIN,
            "productType": config.PRODUCT_TYPE,
        }
        data = self._get("/account/account", params)
        account = data.get("data") or {}
        available = account.get("available") or account.get("availableBalance") or 0
        return float(available)

    def get_open_positions(self) -> list[Position]:
        """Return all open positions for the configured symbol."""
        params = {
            "productType": config.PRODUCT_TYPE,
            "marginCoin": config.MARGIN_COIN,
        }
        data = self._get("/position/all-position", params)
        positions = []
        for p in data.get("data") or []:
            if p.get("symbol") != config.SYMBOL:
                continue
            total = float(p.get("total") or 0)
            if total == 0:
                continue
            side = p.get("holdSide", "").lower()  # "long" | "short"
            eth_size = total * config.CONTRACT_SIZE
            positions.append(Position(
                symbol=p["symbol"],
                side=side,
                size=eth_size,
                entry_price=float(p.get("averageOpenPrice") or p.get("openPriceAvg") or 0),
                unrealised_pnl=float(p.get("unrealisedPL") or p.get("unrealizedPL") or 0),
            ))
        return positions

    def get_open_orders(self) -> list[Order]:
        """Return all open (unfilled) orders for the configured symbol."""
        params = {
            "symbol": config.SYMBOL,
            "productType": config.PRODUCT_TYPE,
        }
        data = self._get("/order/orders-pending", params)
        raw = (data.get("data") or {})
        order_list = raw.get("entrustedList") or raw if isinstance(raw, list) else []
        orders = []
        for o in order_list:
            size_contracts = float(o.get("size") or o.get("qty") or 0)
            orders.append(Order(
                order_id=str(o.get("orderId", "")),
                symbol=o.get("symbol", ""),
                side=o.get("side", "").lower(),
                order_type=o.get("orderType", "market"),
                size=size_contracts * config.CONTRACT_SIZE,
                limit_price=_maybe_float(o.get("price")),
                status=o.get("status", "live"),
            ))
        return orders

    # ──────────────────────────────────────────
    # Order placement
    # ──────────────────────────────────────────

    def place_market_order(
        self,
        side: str,                          # "buy" | "sell"
        size: float,                        # BTC
        reduce_only: bool = False,
        client_id: Optional[str] = None,
        preset_sl: Optional[float] = None,  # embed SL in entry order
        preset_tp: Optional[float] = None,  # embed TP in entry order
    ) -> Optional[str]:
        body = self._base_order_body(side, size, reduce_only, client_id)
        body["orderType"] = "market"
        if preset_sl:
            body["presetStopLossPrice"]    = _fmt_price(preset_sl)
        if preset_tp:
            body["presetStopSurplusPrice"] = _fmt_price(preset_tp)
        return self._send_order(body)

    def place_limit_order(
        self,
        side: str,
        size: float,
        price: float,
        reduce_only: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[str]:
        body = self._base_order_body(side, size, reduce_only, client_id)
        body["orderType"] = "limit"
        body["price"] = _fmt_price(price)
        body["force"] = "gtc"
        return self._send_order(body)

    def place_stop_market_order(
        self,
        side: str,          # "buy" (long position) | "sell" (short position) — matches the
                             # position's direction, same value as the entry order's side.
                             # Bitget's hedge-mode `side` always tracks position direction,
                             # NOT the literal buy/sell action needed to close it —
                             # `tradeSide` (open/close) is what differentiates entry vs exit.
                             # Sending the flipped action side here (the old bug) makes
                             # Bitget label a short's SL/TP as "Close Long".
        size: float,
        stop_price: float,
        trigger: str = "mark_price",  # "mark_price" | "fill_price" | "index_price"
        reduce_only: bool = True,
        client_id: Optional[str] = None,
    ) -> Optional[str]:
        """Stop-market plan order (used as hard stop-loss)."""
        if self._dry_run:
            fake_id = f"DRY_PLAN_{int(time.time() * 1000)}"
            logger.info(f"[DRY] Stop plan order: side={side} size={size} stop={stop_price} → id={fake_id}")
            self._plan_orders.add(fake_id)
            return fake_id

        trade_side = "close" if reduce_only else "open"

        body: dict = {
            "symbol":       config.SYMBOL,
            "productType":  config.PRODUCT_TYPE,
            "marginMode":   config.MARGIN_MODE,
            "marginCoin":   config.MARGIN_COIN,
            "planType":     "normal_plan",
            "size":         _fmt_size(size),
            "triggerPrice": _fmt_price(stop_price),
            "triggerType":  trigger,
            "side":         side,
            "tradeSide":    trade_side,
            "orderType":    "market",
        }
        if client_id:
            body["clientOid"] = client_id

        resp = self._post("/order/place-plan-order", body)
        if _is_success(resp):
            oid = str((resp.get("data") or {}).get("orderId", ""))
            self._plan_orders.add(oid)
            self.last_error = ""
            logger.info(f"SL plan order placed: side={side} size={body.get('size')} stop={stop_price} → id={oid}")
            return oid
        self.last_error = resp.get("msg", "unknown error")
        logger.error(f"SL plan order failed: {body} → {resp}")
        return None

    def place_take_profit_order(
        self,
        side: str,           # "buy" (long position) | "sell" (short position) — see note
                              # on place_stop_market_order: matches position direction,
                              # not the literal closing action.
        size: float,
        limit_price: float,
        reduce_only: bool = True,
        client_id: Optional[str] = None,
    ) -> Optional[str]:
        """Take-profit plan order — shows as TP in Bitget UI."""
        if self._dry_run:
            fake_id = f"DRY_TP_{int(time.time() * 1000)}"
            logger.info(f"[DRY] TP plan order: side={side} size={size} tp={limit_price} → id={fake_id}")
            self._plan_orders.add(fake_id)
            return fake_id

        trade_side = "close" if reduce_only else "open"

        body: dict = {
            "symbol":       config.SYMBOL,
            "productType":  config.PRODUCT_TYPE,
            "marginMode":   config.MARGIN_MODE,
            "marginCoin":   config.MARGIN_COIN,
            "planType":     "normal_plan",
            "size":         _fmt_size(size),
            "triggerPrice": _fmt_price(limit_price),
            "triggerType":  "mark_price",
            "side":         side,
            "tradeSide":    trade_side,
            "orderType":    "market",
        }
        if client_id:
            body["clientOid"] = client_id

        resp = self._post("/order/place-plan-order", body)
        if _is_success(resp):
            oid = str((resp.get("data") or {}).get("orderId", ""))
            self._plan_orders.add(oid)
            self.last_error = ""
            logger.info(f"TP plan order placed: side={side} size={body.get('size')} tp={limit_price} → id={oid}")
            return oid
        self.last_error = resp.get("msg", "unknown error")
        logger.error(f"TP plan order failed: {body} → {resp}")
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel any order by ID (automatically routes to plan-cancel if needed)."""
        if self._dry_run:
            logger.info(f"[DRY] Cancel order {order_id}")
            self._plan_orders.discard(order_id)
            return True

        if order_id in self._plan_orders:
            return self._cancel_plan_order(order_id)

        body = {
            "symbol":      config.SYMBOL,
            "productType": config.PRODUCT_TYPE,
            "orderId":     order_id,
        }
        resp = self._post("/order/cancel-order", body)
        if _is_success(resp):
            logger.info(f"Order cancelled: {order_id}")
            return True
        # Fall back to plan-cancel in case the ID was a plan order we lost track of
        logger.debug(f"Regular cancel failed for {order_id}, trying plan cancel")
        return self._cancel_plan_order(order_id)

    def set_leverage(self, leverage: int) -> bool:
        """Set leverage for the symbol (sets both long and short sides)."""
        if self._dry_run:
            logger.info(f"[DRY] Set leverage {leverage}x")
            return True
        ok = True
        for side in ("long", "short"):
            body = {
                "symbol":      config.SYMBOL,
                "productType": config.PRODUCT_TYPE,
                "marginCoin":  config.MARGIN_COIN,
                "leverage":    str(leverage),
                "holdSide":    side,
            }
            resp = self._post("/account/set-leverage", body)
            if not _is_success(resp):
                logger.warning(f"set-leverage {side}: {resp.get('msg')}")
                ok = False
        if ok:
            logger.info(f"Leverage set to {leverage}x")
        return ok

    def cancel_all_orders(self) -> None:
        for o in self.get_open_orders():
            self.cancel_order(o.order_id)

    def get_closed_position_data(
        self,
        hold_side: str,
        entry_price: Optional[float] = None,
        tolerance_pct: float = 0.005,
        retries: int = 3,
        retry_delay_s: float = 1.5,
    ) -> Optional[dict]:
        """Return the closed position record for hold_side that matches this trade.

        Bitget's history endpoint returns recent closes for the side regardless
        of which trade they belong to. Without correlation, a same-direction
        trade from an earlier session (e.g. London) can be mistaken for the
        trade that just closed if the fresh record hasn't propagated yet —
        silently attaching the wrong PnL/exit price to the new trade. When
        entry_price is given, only a record whose openAvgPrice is within
        tolerance_pct of it is accepted; a few short retries cover the case
        where the real record simply hasn't landed yet.

        Returns a dict with keys: net_pnl, fees, exit_price, entry_price, size_btc
        or None if no matching record is found.
        """
        params = {
            "symbol":      config.SYMBOL,
            "productType": config.PRODUCT_TYPE,
            "limit":       "5",
        }
        for attempt in range(1, retries + 1):
            resp = self._get("/position/history-position", params)
            data = resp.get("data") or {}
            if isinstance(data, dict):
                positions = data.get("list") or []
            elif isinstance(data, list):
                positions = data
            else:
                positions = []
            for pos in positions:
                if pos.get("holdSide", "").lower() != hold_side.lower():
                    continue
                net = pos.get("netProfit") or pos.get("pnl")
                if net is None:
                    continue
                open_avg = float(pos.get("openAvgPrice") or 0)
                if entry_price and open_avg and abs(open_avg - entry_price) / entry_price > tolerance_pct:
                    continue  # doesn't match this trade — likely a prior same-side trade
                open_fee  = abs(float(pos.get("openFee")  or 0))
                close_fee = abs(float(pos.get("closeFee") or 0))
                return {
                    "net_pnl":     float(net),
                    "fees":        open_fee + close_fee,
                    "exit_price":  float(pos.get("closeAvgPrice") or 0),
                    "entry_price": open_avg,
                    "size_btc":    float(pos.get("closeTotalPos") or 0),
                }
            if entry_price and attempt < retries:
                time.sleep(retry_delay_s)
        return None

    def flash_close(self, hold_side: str) -> bool:
        """Close an open position at market using Bitget's dedicated close endpoint.

        hold_side: "long" to close a long position, "short" to close a short.
        This is the same call the Bitget UI makes when you click Close at Market.
        """
        if self._dry_run:
            logger.info(f"[DRY] Flash close {hold_side} position")
            return True
        body = {
            "symbol":      config.SYMBOL,
            "productType": config.PRODUCT_TYPE,
            "holdSide":    hold_side,
        }
        resp = self._post("/order/close-positions", body)
        if _is_success(resp):
            self.last_error = ""
            logger.info(f"Flash close {hold_side} sent: {resp.get('data')}")
            return True
        self.last_error = resp.get("msg", "unknown error")
        logger.error(f"Flash close failed: {resp}")
        return False

    def close_position(self, position: Position) -> Optional[str]:
        ok = self.flash_close(position.side)
        return "ok" if ok else None

    # ──────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────

    def _base_order_body(
        self,
        side: str,
        size_eth: float,
        reduce_only: bool,
        client_id: Optional[str],
    ) -> dict:
        trade_side = "close" if reduce_only else "open"
        body: dict = {
            "symbol":      config.SYMBOL,
            "productType": config.PRODUCT_TYPE,
            "marginMode":  config.MARGIN_MODE,
            "marginCoin":  config.MARGIN_COIN,
            "size":        _fmt_size(size_eth),
            "side":        side,
            "tradeSide":   trade_side,
        }
        if client_id:
            body["clientOid"] = client_id
        return body

    def _send_order(self, body: dict) -> Optional[str]:
        if self._dry_run:
            fake_id = f"DRY_{int(time.time() * 1000)}"
            logger.info(f"[DRY] Order: {body} → id={fake_id}")
            return fake_id
        resp = self._post("/order/place-order", body)
        if _is_success(resp):
            oid = str((resp.get("data") or {}).get("orderId", ""))
            logger.info(
                f"Order placed: {body.get('orderType')} {body.get('side')} "
                f"size={body.get('size')} {body.get('symbol')} → id={oid}"
            )
            self.last_error = ""
            return oid
        self.last_error = resp.get("msg", "unknown error")
        logger.error(f"Order failed: {body} → {resp}")
        return None

    def _cancel_plan_order(self, order_id: str) -> bool:
        body = {
            "symbol":      config.SYMBOL,
            "productType": config.PRODUCT_TYPE,
            "orderId":     order_id,
        }
        resp = self._post("/order/cancel-plan-order", body)
        if _is_success(resp):
            self._plan_orders.discard(order_id)
            logger.info(f"Plan order cancelled: {order_id}")
            return True
        logger.error(f"Failed to cancel plan order {order_id}: {resp}")
        return False

    def _get(self, path: str, params: dict) -> dict:
        qs = urllib.parse.urlencode(params)
        request_path = f"{self._REST_PREFIX}{path}?{qs}" if qs else f"{self._REST_PREFIX}{path}"
        url = f"{self._base}{request_path}"
        headers = self._auth_headers("GET", request_path, "")
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                r = self._session.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT_S)
                if not r.ok:
                    logger.error(f"GET {path} HTTP {r.status_code}: {r.text}")
                    if 400 <= r.status_code < 500:
                        return {}  # client error — don't retry
                    r.raise_for_status()
                resp = r.json()
                if resp.get("code") != "00000":
                    logger.error(f"Bitget API error GET {path}: {resp}")
                    return {}
                return resp
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(f"GET {path} attempt {attempt}: {exc}")
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_DELAY_S * attempt)
        raise last_exc

    def _post(self, path: str, body: dict) -> dict:
        request_path = f"{self._REST_PREFIX}{path}"
        url = f"{self._base}{request_path}"
        body_str = json.dumps(body)
        headers = self._auth_headers("POST", request_path, body_str)
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                r = self._session.post(
                    url, data=body_str, headers=headers,
                    timeout=config.REQUEST_TIMEOUT_S,
                )
                if not r.ok:
                    logger.error(f"POST {path} HTTP {r.status_code}: {r.text}")
                    if 400 <= r.status_code < 500:
                        return r.json() if r.text else {}
                    r.raise_for_status()
                return r.json()
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(f"POST {path} attempt {attempt}: {exc}")
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_DELAY_S * attempt)
        raise last_exc

    def _auth_headers(self, method: str, request_path: str, body: str) -> dict:
        if not self._key or not self._secret or not self._passphrase:
            return {}
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + request_path + body
        sig = base64.b64encode(
            hmac.new(
                self._secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {
            "ACCESS-KEY":        self._key,
            "ACCESS-SIGN":       sig,
            "ACCESS-TIMESTAMP":  timestamp,
            "ACCESS-PASSPHRASE": self._passphrase,
        }


# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _fmt_price(price: float) -> str:
    p = config.PRICE_PRECISION
    return f"{round(price, p):.{p}f}"


def _fmt_size(btc: float) -> str:
    """Round BTC amount to Bitget step size and format as string.

    Bitget USDT-M `size` is in base coin (BTC), not contract count.
    Step / minimum = CONTRACT_SIZE (0.001 BTC for BTCUSDT).
    """
    step = config.CONTRACT_SIZE          # 0.001 BTC
    n    = max(1, round(btc / step))     # at least 1 step
    decimals = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return f"{n * step:.{decimals}f}"


def _is_success(resp: dict) -> bool:
    return resp.get("code") == "00000"


def _maybe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None