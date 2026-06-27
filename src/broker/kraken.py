"""
Kraken Futures authenticated REST client.
Handles account queries, order placement, and position management.

Authentication:
  message  = postData + nonce + endpoint_path
  hash     = SHA-256(message)
  authent  = base64( HMAC-SHA512( b64decode(secret), hash ) )
  headers  = { APIKey, Nonce, Authent }
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests
from loguru import logger

import config


# ──────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    side: str         # "long" | "short"
    size: float       # contracts (positive)
    entry_price: float
    unrealised_pnl: float = 0.0


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    order_type: str   # "lmt" | "mkt" | "stp"
    size: float
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = "open"


# ──────────────────────────────────────────────────────
# Broker
# ──────────────────────────────────────────────────────

class KrakenBroker:
    """Authenticated REST client for Kraken Futures."""

    def __init__(self) -> None:
        self._base = config.KRAKEN_BASE_URL
        self._path = config.KRAKEN_REST_PATH
        self._key = config.KRAKEN_API_KEY
        self._secret = config.KRAKEN_API_SECRET
        self._dry_run = config.DRY_RUN
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        if self._dry_run:
            logger.warning("DRY RUN MODE — no real orders will be placed")
        if not self._key or not self._secret:
            logger.warning("API credentials not set — only public endpoints will work")

    # ──────────────────────────────────────────
    # Account
    # ──────────────────────────────────────────

    def get_account_balance(self) -> float:
        """Return available cash balance in USD."""
        data = self._get("/accounts")
        accounts = data.get("accounts", {})
        cash = accounts.get("cash", accounts.get("fi_ethusd", {}))
        if isinstance(cash, dict):
            balance = cash.get("availableMargin") or cash.get("balance") or 0
        else:
            balance = 0
        # Also check flex account
        flex = accounts.get("flex", {})
        if flex:
            balance = flex.get("availableMargin") or flex.get("balanceValue") or balance
        return float(balance)

    def get_open_positions(self) -> list[Position]:
        """Return all open positions for our symbol."""
        data = self._get("/openpositions")
        positions = []
        for p in data.get("openPositions", []):
            if p.get("symbol") != config.SYMBOL:
                continue
            side = p.get("side", "").lower()
            size = abs(float(p.get("size", 0)))
            if size == 0:
                continue
            positions.append(Position(
                symbol=p["symbol"],
                side=side,
                size=size,
                entry_price=float(p.get("price", 0)),
                unrealised_pnl=float(p.get("unrealisedFunding", 0)),
            ))
        return positions

    def get_open_orders(self) -> list[Order]:
        """Return all open orders for our symbol."""
        data = self._get("/openorders")
        orders = []
        for o in data.get("openOrders", []):
            if o.get("symbol") != config.SYMBOL:
                continue
            orders.append(Order(
                order_id=o.get("order_id") or o.get("orderId", ""),
                symbol=o["symbol"],
                side=o.get("side", "").lower(),
                order_type=o.get("orderType", o.get("type", "mkt")),
                size=float(o.get("qty") or o.get("size") or o.get("quantity", 0)),
                limit_price=_maybe_float(o.get("limitPrice")),
                stop_price=_maybe_float(o.get("stopPrice")),
                status=o.get("status", "open"),
            ))
        return orders

    # ──────────────────────────────────────────
    # Order placement
    # ──────────────────────────────────────────

    def place_market_order(
        self,
        side: str,              # "buy" | "sell"
        size: float,            # contracts
        reduce_only: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[str]:
        """Market order — immediate fill at best available price."""
        params: dict = {
            "orderType": "mkt",
            "symbol": config.SYMBOL,
            "side": side,
            "size": _fmt_size(size),
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if client_id:
            params["cliOrdId"] = client_id
        return self._send_order(params)

    def place_limit_order(
        self,
        side: str,
        size: float,
        price: float,
        reduce_only: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[str]:
        """Limit order (post-only eligible for lower fees)."""
        params: dict = {
            "orderType": "lmt",
            "symbol": config.SYMBOL,
            "side": side,
            "size": _fmt_size(size),
            "limitPrice": f"{price:.4f}",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if client_id:
            params["cliOrdId"] = client_id
        return self._send_order(params)

    def place_stop_market_order(
        self,
        side: str,
        size: float,
        stop_price: float,
        trigger: str = "mark",   # "mark" | "index" | "last"
        reduce_only: bool = True,
        client_id: Optional[str] = None,
    ) -> Optional[str]:
        """Stop market order for hard stop-loss."""
        params: dict = {
            "orderType": "stp",
            "symbol": config.SYMBOL,
            "side": side,
            "size": _fmt_size(size),
            "stopPrice": f"{stop_price:.4f}",
            "triggerSignal": trigger,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if client_id:
            params["cliOrdId"] = client_id
        return self._send_order(params)

    def place_take_profit_order(
        self,
        side: str,
        size: float,
        limit_price: float,
        reduce_only: bool = True,
        client_id: Optional[str] = None,
    ) -> Optional[str]:
        """Limit order used as take-profit."""
        return self.place_limit_order(side, size, limit_price, reduce_only, client_id)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID. Returns True on success."""
        if self._dry_run:
            logger.info(f"[DRY] Cancel order {order_id}")
            return True
        data = self._post("/cancelorder", {"order_id": order_id})
        result = data.get("result", "error")
        if result == "success":
            logger.info(f"Cancelled order {order_id}")
            return True
        logger.error(f"Failed to cancel order {order_id}: {data}")
        return False

    def cancel_all_orders(self) -> None:
        """Cancel all open orders for our symbol."""
        open_orders = self.get_open_orders()
        for o in open_orders:
            self.cancel_order(o.order_id)

    def close_position(self, position: Position) -> Optional[str]:
        """Flatten an open position at market price."""
        close_side = "buy" if position.side == "short" else "sell"
        return self.place_market_order(
            side=close_side,
            size=position.size,
            reduce_only=True,
            client_id="close_pos",
        )

    # ──────────────────────────────────────────
    # Internal REST helpers
    # ──────────────────────────────────────────

    def _send_order(self, params: dict) -> Optional[str]:
        if self._dry_run:
            fake_id = f"DRY_{int(time.time() * 1000)}"
            logger.info(f"[DRY] Order: {params} → id={fake_id}")
            return fake_id
        data = self._post("/sendorder", params)
        if data.get("result") == "success":
            oid = (
                data.get("sendStatus", {}).get("order_id")
                or data.get("sendStatus", {}).get("orderId")
                or data.get("order_id")
            )
            logger.info(f"Order placed: {params.get('orderType')} {params.get('side')} "
                        f"{params.get('size')} {params.get('symbol')} → id={oid}")
            return oid
        logger.error(f"Order failed: {params} → {data}")
        return None

    def _get(self, endpoint: str) -> dict:
        url = f"{self._base}{self._path}{endpoint}"
        headers = self._auth_headers("GET", endpoint, "")
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                r = self._session.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT_S)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(f"GET {endpoint} attempt {attempt}: {exc}")
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_DELAY_S * attempt)
        raise last_exc

    def _post(self, endpoint: str, params: dict) -> dict:
        url = f"{self._base}{self._path}{endpoint}"
        body = urllib.parse.urlencode(params)
        headers = self._auth_headers("POST", endpoint, body)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                r = self._session.post(url, data=body, headers=headers,
                                       timeout=config.REQUEST_TIMEOUT_S)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(f"POST {endpoint} attempt {attempt}: {exc}")
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_DELAY_S * attempt)
        raise last_exc

    def _auth_headers(self, method: str, endpoint: str, post_data: str) -> dict:
        if not self._key or not self._secret:
            return {}
        try:
            secret_bytes = base64.b64decode(self._secret)
        except Exception:
            logger.error("KRAKEN_API_SECRET is not valid base64 — check your .env file")
            return {}
        nonce = str(int(time.time() * 1000))
        full_endpoint = f"{self._path}{endpoint}"
        message = post_data + nonce + full_endpoint
        sha256 = hashlib.sha256(message.encode("utf-8")).digest()
        sig = hmac.new(secret_bytes, sha256, hashlib.sha512).digest()
        authent = base64.b64encode(sig).decode("utf-8")
        return {
            "APIKey": self._key,
            "Nonce": nonce,
            "Authent": authent,
        }


# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _maybe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fmt_size(size: float) -> str:
    # Kraken expects size as a number string; truncate to 4 decimal places
    return f"{size:.4f}"
