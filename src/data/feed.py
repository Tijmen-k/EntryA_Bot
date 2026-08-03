"""
Bitget Futures public data feed.
Fetches OHLCV candles and ticker via REST — no authentication needed for market data.
"""
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from loguru import logger

import config


@dataclass
class Bar:
    timestamp: datetime   # UTC, bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float


# Bitget granularity strings
_RESOLUTION_MAP = {
    "1m":  "1m",
    "3m":  "3m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1H",
    "2h":  "2H",
    "4h":  "4H",
    "6h":  "6H",
    "12h": "12H",
    "1d":  "1D",
    "1w":  "1W",
}

_REST_PREFIX = "/api/v2/mix"


class BitgetFeed:
    """Public REST client for Bitget USDT-M Futures OHLCV and ticker data."""

    def __init__(self) -> None:
        self._base    = config.BITGET_BASE_URL
        self._session = requests.Session()
        self._session.headers.update({
            "Accept":  "application/json",
            "locale":  "en-US",
        })

    # ──────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────

    def fetch_ohlcv(
        self,
        symbol: str,
        resolution: str = "1m",
        count: int = 300,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> list[Bar]:
        """Return up to `count` bars sorted oldest → newest."""
        granularity = _RESOLUTION_MAP.get(resolution.lower(), resolution)
        params: dict = {
            "symbol":      symbol,
            "productType": config.PRODUCT_TYPE,
            "granularity": granularity,
            "limit":       str(min(count, 1000)),
        }
        if from_ts:
            params["startTime"] = str(from_ts)
        if to_ts:
            params["endTime"] = str(to_ts)

        data = self._get("/market/candles", params)
        raw = data.get("data") or []
        if not raw:
            logger.warning(f"Empty candles response for {symbol}")
            return []

        bars: list[Bar] = []
        for c in raw:
            try:
                ts_ms = int(c[0])
                bars.append(Bar(
                    timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    open=float(c[1]),
                    high=float(c[2]),
                    low=float(c[3]),
                    close=float(c[4]),
                    volume=float(c[5]),
                ))
            except (IndexError, ValueError, TypeError) as exc:
                logger.debug(f"Skipping malformed candle entry: {c} ({exc})")

        bars.sort(key=lambda b: b.timestamp)
        return bars

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Return mid price from the best bid/ask, falling back to last price."""
        params = {
            "symbol":      symbol,
            "productType": config.PRODUCT_TYPE,
        }
        data = self._get("/market/ticker", params)
        ticker = data.get("data") or {}
        # Bitget may return a list (all tickers) or a single dict
        if isinstance(ticker, list):
            ticker = next((t for t in ticker if t.get("symbol") == symbol), {})
        bid  = _maybe_float(ticker.get("bidPr")  or ticker.get("bestBid"))
        ask  = _maybe_float(ticker.get("askPr")  or ticker.get("bestAsk"))
        last = _maybe_float(ticker.get("lastPr") or ticker.get("last") or ticker.get("markPrice"))
        if bid and ask:
            return (bid + ask) / 2
        return last

    # ──────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────

    def _get(self, path: str, params: dict) -> dict:
        qs = urllib.parse.urlencode(params)
        url = f"{self._base}{_REST_PREFIX}{path}?{qs}"
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                r = self._session.get(url, timeout=config.REQUEST_TIMEOUT_S)
                if not r.ok:
                    # Log the full Bitget error body so we can see the exact error code/msg
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
                logger.warning(f"GET {path} attempt {attempt}: {exc}")
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_DELAY_S * attempt)
        logger.error(f"All retries exhausted for GET {path}")
        return {}


def _maybe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None
